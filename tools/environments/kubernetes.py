"""Kubernetes pod execution environment.

One pod per (profile, session) in the namespace the PROFILE config names (``terminal.kubernetes``),
never one the model or a command chooses. ``/workspace`` is an EXISTING claim (``workspace_pvc``,
optionally at ``workspace_subpath``) or, without one, an emptyDir: the backend never creates or deletes
a PersistentVolumeClaim, so it needs only pod + pods/exec rights. Idling out or cleaning up deletes
the pod. Commands run spawn-per-call through the pod exec websocket
(``kubernetes.stream``) under the base class's snapshot/CWD wrapping, like the other remote backends.
"""

import hashlib
import io
import logging
import re
import tarfile
import threading
import time
import uuid
import weakref
from datetime import UTC, datetime
from pathlib import Path

from tools.environments.base import BaseEnvironment, EnvironmentConnectionError
from tools.environments.base_output import _ThreadedProcessHandle
from tools.environments.file_sync import FileSyncManager, iter_sync_files, quoted_rm_command
from tools.environments.remote_common import ensure_lazy_dep

logger = logging.getLogger(__name__)

# Tests patch these instead of the shared ``time`` module.
_sleep = time.sleep
_monotonic = time.monotonic

WORKSPACE = "/workspace"
# The pod's own HOME (an emptyDir): ~/.hermes sync lands here, never on a shared workspace claim.
HOME_DIR = "/home/hermes"
DEFAULTS = {"cpu": "1", "memory": "2Gi", "ephemeral_storage": "10Gi", "idle_timeout": 1800, "context": "",
            "kubeconfig": "", "labels": {}, "workspace_pvc": "", "workspace_subpath": "", "workspace_mount": WORKSPACE,
            "run_as_user": 1000,
            "run_as_group": 1000, "fs_group": 1000, "node_selector": {}, "tolerations": []}
MISSING_EXTRA_MESSAGE = ("The kubernetes terminal backend needs the optional 'kubernetes' extra "
                         "(run `hermes setup terminal` and pick Kubernetes, or install hermes-agent[kubernetes])")
MANAGED_BY_LABEL = ("app.kubernetes.io/managed-by", "hermes")
PROFILE_LABEL = "hermes/profile"
LAST_USED_ANNOTATION = "hermes/last-used"
_CONTAINER = "workspace"
_READY_TIMEOUT_S = 300
_POLL_S = 1.0
_STAMP_EVERY_S = 60.0
_CANCEL_GRACE_S = 5.0
_STDIN_CHANNEL = 0
# Waiting reasons that never resolve by themselves: fail fast instead of burning the Ready timeout.
_FATAL_WAITING = frozenset({"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull",
                            "CreateContainerConfigError", "CreateContainerError", "RunContainerError"})
# PID 1 traps TERM so a pod delete finishes at once instead of waiting out the grace period.
_KEEPALIVE = "trap 'exit 0' TERM; while :; do sleep 3600 & wait $!; done"
# $1 pidfile, $2 stdin byte count, $3 command, $4 "-l" or "". The pidfile names the wrapper so a
# timeout can kill the whole tree; stdin is length-framed (``head -c``) so EOF needs no channel close.
_RUN_WRAPPER = """echo $$ > "${1:?}" || exit 125
if [ "$2" -gt 0 ]; then
  # no-tmp: ok — pod-side path inside the sandbox, never the host
  f="/tmp/.hermes-stdin-$$"
  (umask 077; head -c "$2" > "$f") || exit 125
  exec 0< "$f"
  rm -f -- "${f:?}"
else
  exec 0< /dev/null
fi
bash $4 -c "$3" 2>&1
rc=$?
rm -f -- "${1:?}"
exit $rc
"""
# Freeze each process before walking its children so the tree cannot grow while it is killed.
_KILL_TREE = """p=$(cat "${1:?}" 2>/dev/null) || exit 0
kt() {
  local c
  kill -STOP "$1" 2>/dev/null
  for c in $(cat /proc/"$1"/task/*/children 2>/dev/null || pgrep -P "$1" 2>/dev/null); do kt "$c"; done
  kill -KILL "$1" 2>/dev/null
}
kt "$p"
rm -f -- "${1:?}"
"""


def dns_label(text: str, max_len: int = 63) -> str:
    """``text`` as a DNS-1123 label fragment (lowercase alnum and '-', no edge dashes)."""
    label = re.sub(r"[^a-z0-9-]+", "-", str(text).lower()).strip("-")
    return label[:max_len].strip("-") or "default"


def pod_name(profile: str, session: str) -> str:
    """Deterministic pod name for one (profile, session): ``hermes-<profile>-<10 hex>``, <= 63 chars."""
    digest = hashlib.sha256(str(session).encode()).hexdigest()[:10]
    return f"hermes-{dns_label(profile, 63 - len('hermes--') - len(digest))}-{digest}"


def settings_from_config(raw: dict) -> dict:
    """Constructor kwargs from the profile's ``terminal.kubernetes`` section (defaults filled in)."""
    raw = raw if isinstance(raw, dict) else {}
    settings = {key: raw[key] if raw.get(key) not in (None, "") else default for key, default in DEFAULTS.items()}
    settings.update(labels=dict(settings["labels"] or {}), node_selector=dict(settings["node_selector"] or {}),
                    tolerations=list(settings["tolerations"] or []))
    settings.update(namespace=str(raw.get("namespace") or "").strip(), image=str(raw.get("image") or "").strip())
    return settings


def _load_kubernetes(context: str, kubeconfig: str = ""):
    """(CoreV1Api, stream fn, ApiException). An explicit ``kubeconfig`` path wins; otherwise in-cluster
    auth, else KUBECONFIG / ~/.kube/config. ``context`` picks a kubeconfig context."""
    try:
        ensure_lazy_dep("kubernetes")
        from kubernetes import client, config
        from kubernetes.client.exceptions import ApiException
        from kubernetes.stream import stream
    except ImportError as exc:
        raise ImportError(f"{MISSING_EXTRA_MESSAGE}: {exc}") from exc
    try:
        if kubeconfig:
            config.load_kube_config(config_file=str(Path(kubeconfig).expanduser()), context=context or None)
        else:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config(context=context or None)
    except (config.ConfigException, OSError) as exc:
        raise EnvironmentConnectionError(f"Kubernetes: no usable kubeconfig or in-cluster config: {exc}") from exc
    _mirror_bearer_token(client)
    return client.CoreV1Api(), stream, ApiException


def _mirror_bearer_token(client) -> None:
    """kubernetes 36.0.0's loaders store the token under ``api_key['authorization']`` but its
    ``auth_settings()`` reads only ``api_key['BearerToken']``, so requests go without a token and the
    API server answers 401. Mirror the key on the default configuration, and again after every
    token refresh (in-cluster and exec tokens rotate). A no-op for clients that read 'authorization'."""
    cfg_cls = getattr(client, "Configuration", None)
    if cfg_cls is None:
        return
    cfg = cfg_cls.get_default_copy()
    if "authorization" not in cfg.api_key or "BearerToken" in cfg.api_key:
        return
    inner = cfg.refresh_api_key_hook

    def refresh(c) -> None:
        if inner is not None:
            inner(c)
        if "authorization" in c.api_key:
            c.api_key["BearerToken"] = c.api_key["authorization"]

    cfg.api_key["BearerToken"] = cfg.api_key["authorization"]
    cfg.refresh_api_key_hook = refresh
    cfg_cls.set_default(cfg)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pod_failure(pod) -> str | None:
    """Why *pod* can never become Ready, or None while it still might."""
    status = pod.status
    if status.phase in ("Failed", "Succeeded"):
        return f"pod phase is {status.phase}"
    for cs in status.container_statuses or ():
        waiting = cs.state.waiting if cs.state else None
        if waiting is not None and waiting.reason in _FATAL_WAITING:
            return f"container {waiting.reason}: {waiting.message or ''}".strip()
    return None


def _pod_pending_detail(pod) -> str:
    for cond in pod.status.conditions or ():
        if cond.type == "PodScheduled" and cond.status != "True":
            return f"not scheduled ({cond.reason}): {cond.message or ''}".strip()
    return f"phase {pod.status.phase}"


def _pod_ready(pod) -> bool:
    return any(c.type == "Ready" and c.status == "True" for c in pod.status.conditions or ())


def _idle_watch(env_ref, closed: threading.Event, idle_timeout: float) -> None:
    """Reap the env's pod once idle. Holds only a weak reference so the env can still be collected."""
    while not closed.wait(max(0.25, min(60.0, idle_timeout / 4))):
        env = env_ref()
        if env is None:
            return
        try:
            env._reap_if_idle()
        except EnvironmentConnectionError:
            logger.warning("Kubernetes: idle reap of %s failed; will retry", env.pod_name, exc_info=True)
        del env


class KubernetesEnvironment(BaseEnvironment):
    """Per-session pod; see the module docstring."""

    _stdin_mode = "payload"  # length-framed over the exec stdin channel (see _RUN_WRAPPER)
    _persistent = True  # the pod lives across turns; idle_timeout / cleanup() delete it

    def __init__(self, *, namespace: str, image: str, profile: str, task_id: str = "default", cwd: str = WORKSPACE,
                 timeout: int = 60, cpu: str = "1", memory: str = "2Gi", ephemeral_storage: str = "10Gi",
                 idle_timeout: int = 1800, context: str = "", kubeconfig: str = "", labels: dict | None = None,
                 workspace_pvc: str = "", workspace_subpath: str = "", workspace_mount: str = WORKSPACE,
                 run_as_user: int = 1000,
                 run_as_group: int = 1000, fs_group: int = 1000, node_selector: dict | None = None,
                 tolerations: list | None = None):
        if not namespace or not image:
            raise ValueError("Kubernetes backend requires terminal.kubernetes.namespace and terminal.kubernetes.image "
                             "in the profile's config.yaml")
        if int(run_as_user) == 0:
            raise ValueError("terminal.kubernetes.run_as_user must be non-zero: pods run with runAsNonRoot")
        super().__init__(cwd=cwd or WORKSPACE, timeout=timeout)
        self._core, self._stream, self._api_exc = _load_kubernetes(context, kubeconfig)
        self.namespace, self.image, self.profile = namespace, image, profile
        self.pod_name = pod_name(profile, task_id)
        self.workspace_pvc, self._workspace_subpath = str(workspace_pvc or ""), str(workspace_subpath or "")
        self._workspace_mount = str(workspace_mount or WORKSPACE)
        if not self._workspace_mount.startswith("/") or self._workspace_mount.rstrip("/") in ("", HOME_DIR):
            raise ValueError(f"Kubernetes: workspace_mount must be an absolute path other than / and {HOME_DIR}")
        self._resources = {"cpu": str(cpu), "memory": str(memory), "ephemeral-storage": str(ephemeral_storage)}
        self._ids = (int(run_as_user), int(run_as_group), int(fs_group))
        self._node_selector, self._tolerations = dict(node_selector or {}), list(tolerations or [])
        self._idle_timeout = float(idle_timeout)
        self._labels = {**{str(k): str(v) for k, v in (labels or {}).items()},
                        MANAGED_BY_LABEL[0]: MANAGED_BY_LABEL[1], PROFILE_LABEL: dns_label(profile)}
        # _lock serialises pod lifecycle (it is held across init_session); _use_lock guards only the
        # in-flight/last-used counters, which exec workers update without waiting on _lock.
        self._lock = threading.RLock()
        self._use_lock = threading.Lock()
        self._inflight = 0
        self._last_used = _monotonic()
        self._last_stamp = 0.0
        self._pod_live = False
        self._closed = threading.Event()
        self._ensure_pod()
        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files(f"{HOME_DIR}/.hermes"), upload_fn=self._upload,
            delete_fn=self._delete_remote, bulk_upload_fn=self._bulk_upload, bulk_download_fn=self._bulk_download)
        self._sync_manager.sync(force=True)
        self.init_session()
        from agent.memory_provider import spawn_context_thread
        spawn_context_thread(_idle_watch, args=(weakref.ref(self), self._closed, self._idle_timeout),
                             name=f"k8s-idle-{self.pod_name}").start()

    # --- Kubernetes objects ---
    def _read(self, fn, name):
        try:
            return fn(name, self.namespace)
        except self._api_exc as exc:
            if exc.status == 404:
                return None
            raise EnvironmentConnectionError(f"Kubernetes API error reading {name}: {exc.status} {exc.reason}") from exc

    def _create(self, fn, body) -> None:
        try:
            fn(self.namespace, body)
        except self._api_exc as exc:
            if exc.status != 409:  # 409: a concurrent creator won the race; use theirs
                raise EnvironmentConnectionError(
                    f"Kubernetes: could not create {body['kind']} {body['metadata']['name']}: {exc.status} {exc.reason}") from exc

    def _workspace_volume(self) -> tuple[dict, dict]:
        """(volume, mount): the configured existing claim (never created here), else an emptyDir."""
        mount = {"name": "workspace", "mountPath": self._workspace_mount}
        if not self.workspace_pvc:
            return {"name": "workspace", "emptyDir": {}}, mount
        if self._workspace_subpath:
            mount["subPath"] = self._workspace_subpath
        return {"name": "workspace", "persistentVolumeClaim": {"claimName": self.workspace_pvc}}, mount

    def pod_spec(self) -> dict:
        uid, gid, fs_group = self._ids
        seccomp = {"type": "RuntimeDefault"}
        volume, mount = self._workspace_volume()
        container = {
            "name": _CONTAINER, "image": self.image, "command": ["sh", "-c", _KEEPALIVE],
            "workingDir": self._workspace_mount, "env": [{"name": "HOME", "value": HOME_DIR}],
            "resources": {"requests": dict(self._resources), "limits": dict(self._resources)},
            "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": gid,
                                "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                                "seccompProfile": seccomp},
            "volumeMounts": [mount, {"name": "home", "mountPath": HOME_DIR}]}
        spec = {"automountServiceAccountToken": False, "restartPolicy": "Never", "terminationGracePeriodSeconds": 5,
                "containers": [container], "volumes": [volume, {"name": "home", "emptyDir": {}}],
                "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": gid, "fsGroup": fs_group,
                                    "seccompProfile": seccomp}}
        if self._node_selector:
            spec["nodeSelector"] = dict(self._node_selector)
        if self._tolerations:
            spec["tolerations"] = list(self._tolerations)
        return {"apiVersion": "v1", "kind": "Pod",
                "metadata": {"name": self.pod_name, "namespace": self.namespace, "labels": dict(self._labels),
                             "annotations": {LAST_USED_ANNOTATION: _now_iso()}},
                "spec": spec}

    def _ensure_pod(self) -> None:
        """Reuse the session's running pod or create it; block until Ready."""
        pod = self._read(self._core.read_namespaced_pod, self.pod_name)
        if pod is not None and (pod.metadata.deletion_timestamp or _pod_failure(pod)):
            self._delete_pod(wait=True)
            pod = None
        if pod is None:
            self._create(self._core.create_namespaced_pod, self.pod_spec())
            logger.info("Kubernetes: created pod %s/%s", self.namespace, self.pod_name)
        self._wait_ready()
        self._pod_live = True
        self._stamp(force=True)

    def _wait_ready(self) -> None:
        deadline = _monotonic() + _READY_TIMEOUT_S
        while True:
            pod = self._read(self._core.read_namespaced_pod, self.pod_name)
            if pod is None:
                raise EnvironmentConnectionError(f"Kubernetes: pod {self.pod_name} disappeared while starting")
            failure = _pod_failure(pod)
            if failure:
                raise EnvironmentConnectionError(f"Kubernetes: pod {self.namespace}/{self.pod_name} cannot start: {failure}")
            if _pod_ready(pod):
                return
            if _monotonic() > deadline:
                raise EnvironmentConnectionError(f"Kubernetes: pod {self.namespace}/{self.pod_name} not Ready after "
                                                 f"{_READY_TIMEOUT_S}s: {_pod_pending_detail(pod)}")
            _sleep(_POLL_S)

    def _delete_pod(self, *, wait: bool = False) -> None:
        try:
            self._core.delete_namespaced_pod(self.pod_name, self.namespace, grace_period_seconds=5)
        except self._api_exc as exc:
            if exc.status != 404:
                raise EnvironmentConnectionError(f"Kubernetes: could not delete pod {self.pod_name}: {exc.status} {exc.reason}") from exc
        self._pod_live = False
        deadline = _monotonic() + _READY_TIMEOUT_S
        while wait and self._read(self._core.read_namespaced_pod, self.pod_name) is not None and _monotonic() < deadline:
            _sleep(_POLL_S)

    def _stamp(self, *, force: bool = False) -> None:
        """Annotate ``hermes/last-used`` (at most once a minute) for an external reaper."""
        if not force and _monotonic() - self._last_stamp < _STAMP_EVERY_S:
            return
        self._last_stamp = _monotonic()
        patch = {"metadata": {"annotations": {LAST_USED_ANNOTATION: _now_iso()}}}
        try:
            self._core.patch_namespaced_pod(self.pod_name, self.namespace, patch)
        except self._api_exc as exc:
            logger.warning("Kubernetes: could not stamp %s on %s: %s", LAST_USED_ANNOTATION, self.pod_name, exc.reason)

    # --- Idle reaping ---
    def _reap_if_idle(self) -> bool:
        """Delete the pod once idle past ``idle_timeout``; the next command recreates it."""
        with self._lock:
            with self._use_lock:
                idle = not self._inflight and _monotonic() - self._last_used >= self._idle_timeout
            if not (self._pod_live and idle):
                return False
            logger.info("Kubernetes: pod %s idle for %.0fs; deleting it", self.pod_name, self._idle_timeout)
            self._delete_pod()
            return True

    def _touch(self, delta: int = 0) -> None:
        with self._use_lock:
            self._inflight += delta
            self._last_used = _monotonic()

    def _before_execute(self) -> None:
        with self._lock:
            self._touch()
            pod = self._read(self._core.read_namespaced_pod, self.pod_name) if self._pod_live else None
            if pod is None or pod.metadata.deletion_timestamp or _pod_failure(pod) or not _pod_ready(pod):
                self._ensure_pod()
                self._mark_recreated()
                self._snapshot_ready = False
                self.init_session()
                self._sync_manager.sync(force=True)
            else:
                self._stamp()
        self._sync_manager.sync()

    # --- Exec ---
    def _exec(self, argv: list[str], stdin: bytes | None = None, *, stop=None) -> tuple[bytes, int]:
        """Run *argv* in the pod; ``(stdout + stderr bytes, exit code)``. 255 = no exit status (stream lost)."""
        resp = self._stream(self._core.connect_get_namespaced_pod_exec, self.pod_name, self.namespace,
                            command=argv, container=_CONTAINER, stdin=stdin is not None, stdout=True, stderr=True,
                            tty=False, _preload_content=False, binary=True)
        out, err = bytearray(), bytearray()
        try:
            if stdin is not None:
                resp.write_stdin(stdin)
                resp.close_channel(_STDIN_CHANNEL)
            while resp.is_open() and not (stop and stop()):
                resp.update(timeout=0.2)
                out += resp.read_stdout(timeout=0)
                err += resp.read_stderr(timeout=0)
            out += resp.read_stdout(timeout=0)
            err += resp.read_stderr(timeout=0)
            if resp.is_open():
                return bytes(out + err), 137
            try:
                code = resp.returncode
            except (TypeError, KeyError, IndexError, ValueError):
                code = None
            if code is None:
                return bytes(out + err) + b"\n[kubernetes exec stream closed without an exit status]", 255
            return bytes(out + err), int(code)
        finally:
            resp.close()

    def _exec_checked(self, argv: list[str], stdin: bytes | None = None) -> bytes:
        try:
            output, code = self._exec(argv, stdin)
        except self._api_exc as exc:
            raise EnvironmentConnectionError(f"Kubernetes exec {argv[:3]} failed: {exc.status} {exc.reason}") from exc
        if code != 0:
            raise EnvironmentConnectionError(f"Kubernetes exec {argv[:3]} failed ({code}): "
                                             f"{output[-500:].decode('utf-8', 'replace')}")
        return output

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120, stdin_data: str | None = None):
        payload = (stdin_data or "").encode("utf-8", "surrogateescape")
        pidfile = f"{self.get_temp_dir()}/hermes-k8s-{uuid.uuid4().hex[:12]}.pid"
        argv = ["bash", "-c", _RUN_WRAPPER, "hermes", pidfile, str(len(payload)), cmd_string, "-l" if login else ""]
        cancelled_at: list[float] = []

        def stop() -> bool:  # cancel() killed the tree; give the stream a grace period to report it
            return bool(cancelled_at) and _monotonic() - cancelled_at[0] > _CANCEL_GRACE_S

        def exec_fn() -> tuple[str, int]:
            self._touch(+1)
            try:
                output, code = self._exec(argv, payload or None, stop=stop)
            except self._api_exc as exc:
                output, code = f"[kubernetes exec failed: {exc.status} {exc.reason}]".encode(), 255
            finally:
                self._touch(-1)
            return output.decode("utf-8", "replace"), code

        def cancel() -> None:
            started = _monotonic()
            cancelled_at.append(started)
            try:
                self._exec(["bash", "-c", _KILL_TREE, "hermes", pidfile],
                           stop=lambda: _monotonic() - started > _CANCEL_GRACE_S * 3)
            except self._api_exc as exc:
                logger.warning("Kubernetes: killing timed-out command in %s failed: %s", self.pod_name, exc.reason)

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel)

    # --- File sync (host ~/.hermes -> pod $HOME/.hermes) ---
    def _upload(self, host_path: str, remote_path: str) -> None:
        data = Path(host_path).read_bytes()
        self._exec_checked(["sh", "-c", 'mkdir -p "$(dirname "$1")" && head -c "$2" > "$1"', "sh",
                            remote_path, str(len(data))], data)

    def _bulk_upload(self, files: list[tuple[str, str]]) -> None:
        if not files:
            return
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for host_path, remote_path in files:
                tar.add(host_path, arcname=remote_path.lstrip("/"), recursive=False)
        data = buf.getvalue()
        self._exec_checked(["sh", "-c", 'head -c "$1" | tar xf - -C /', "sh", str(len(data))], data)

    def _bulk_download(self, dest: Path) -> None:
        tar = self._exec_checked(["tar", "cf", "-", "--exclude=*.sock", "-C", "/", f"{HOME_DIR.lstrip('/')}/.hermes"])
        Path(dest).write_bytes(tar)

    def _delete_remote(self, remote_paths: list[str]) -> None:
        self._exec_checked(["sh", "-c", quoted_rm_command(remote_paths)])

    def cleanup(self):
        """Delete the session pod (a workspace claim is never touched)."""
        closed = getattr(self, "_closed", None)
        if closed is None or closed.is_set():
            return
        closed.set()
        with self._lock:
            if not self._pod_live:
                return
            try:
                self._sync_manager.sync_back()
            except (OSError, RuntimeError) as exc:
                logger.warning("Kubernetes: sync_back from %s failed: %s", self.pod_name, exc)
            try:
                self._delete_pod()
                logger.info("Kubernetes: deleted pod %s/%s", self.namespace, self.pod_name)
            except EnvironmentConnectionError as exc:
                logger.warning("Kubernetes: cleanup of %s failed: %s", self.pod_name, exc)
