"""Behaviour contracts for the ``kubernetes`` terminal backend against a fake cluster.

The fake ``kubernetes`` package keeps pods/PVCs in dicts and runs each pod exec as a real local
``bash`` (``/workspace`` and ``/tmp`` mapped into a temp dir), so command wrapping, cwd tracking,
exit codes and the timeout kill path are exercised end to end without a cluster.
"""

import json
import re
import subprocess
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
import hermes_yaml as yaml

from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("linux", "macos")  # pod execs run as local bash


class FakeApiException(Exception):
    def __init__(self, status=0, reason=""):
        super().__init__(f"{status} {reason}")
        self.status, self.reason = status, reason


class FakeConfigException(Exception):
    pass


def _pod_view(body):
    waiting = body.get("fake_waiting")  # e.g. "ImagePullBackOff": the container never starts
    statuses = [SimpleNamespace(state=SimpleNamespace(waiting=SimpleNamespace(reason=waiting, message="pull failed")))]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=body["metadata"]["name"], deletion_timestamp=None),
        status=SimpleNamespace(
            phase=body.get("fake_phase", "Pending" if waiting else "Running"),
            container_statuses=statuses if waiting else [],
            conditions=[] if waiting else [SimpleNamespace(type="Ready", status="True", reason=None, message=None)]))


class FakeExec:
    """WSClient surface (binary mode) over a local process standing in for the pod exec."""

    def __init__(self, cluster, argv, stdin):
        self.argv = argv
        self._cluster = cluster
        self.proc = subprocess.Popen(
            [cluster.to_local(a) for a in argv], cwd=cluster.workspace, start_new_session=True,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(cluster.workspace), "LANG": "C.UTF-8"},
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._buf = {1: bytearray(), 2: bytearray()}
        self._lock = threading.Lock()
        self._readers = [threading.Thread(target=self._pump, args=(ch, pipe), daemon=True)
                         for ch, pipe in ((1, self.proc.stdout), (2, self.proc.stderr))]
        for t in self._readers:
            t.start()

    def _pump(self, channel, pipe):
        for chunk in iter(lambda: pipe.read1(4096), b""):
            with self._lock:
                self._buf[channel] += chunk

    def write_stdin(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def close_channel(self, channel):
        assert channel == 0
        self.proc.stdin.close()

    def is_open(self):
        return self.proc.poll() is None or any(t.is_alive() for t in self._readers)

    def update(self, timeout=0):
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        if self.proc.poll() is not None:
            for t in self._readers:
                t.join(timeout)

    def _read(self, channel):
        with self._lock:
            data, self._buf[channel] = bytes(self._buf[channel]), bytearray()
        return self._cluster.from_local(data)

    def read_stdout(self, timeout=None):
        return self._read(1)

    def read_stderr(self, timeout=None):
        return self._read(2)

    @property
    def returncode(self):
        return None if self.is_open() else self.proc.returncode

    def close(self):
        pass


class FakeCluster:
    """CoreV1Api subset + the stream() entry point, recording every mutating call."""

    def __init__(self, root: Path):
        self.workspace, self.tmp, self.home = root / "workspace", root / "tmp", root / "home"
        self.workspace.mkdir()
        self.tmp.mkdir()
        self.home.mkdir()
        self.pods, self.calls, self.execs = {}, [], []
        self.kube_context = self.kubeconfig_file = None
        self.fake_waiting = None  # applied to pods created from now on

    def to_local(self, text):  # one pass: the temp root itself may contain "/tmp"
        local = {"/workspace": self.workspace, "/tmp": self.tmp, "/home/hermes": self.home}
        return re.sub(r"/home/hermes|/workspace|/tmp", lambda m: str(local[m.group()]), text)

    def from_local(self, data: bytes) -> bytes:
        return (data.replace(str(self.home).encode(), b"/home/hermes").replace(str(self.workspace).encode(), b"/workspace")
                .replace(str(self.tmp).encode(), b"/tmp"))

    def _get(self, store, namespace, name):
        if (namespace, name) not in store:
            raise FakeApiException(404, "Not Found")
        return store[(namespace, name)]

    def read_namespaced_pod(self, name, namespace):
        return _pod_view(self._get(self.pods, namespace, name))

    def create_namespaced_pod(self, namespace, body):
        self.calls.append(("create_pod", namespace, body["metadata"]["name"]))
        self.pods[(namespace, body["metadata"]["name"])] = {**body, "fake_waiting": self.fake_waiting}

    def delete_namespaced_pod(self, name, namespace, grace_period_seconds=None):
        self.calls.append(("delete_pod", namespace, name))
        self.pods.pop((namespace, name), None)

    def patch_namespaced_pod(self, name, namespace, body):
        pod = self._get(self.pods, namespace, name)
        pod["metadata"].setdefault("annotations", {}).update(body["metadata"]["annotations"])

    # The backend's ServiceAccount has no PVC rights: any claim API call is a contract violation.
    def read_namespaced_persistent_volume_claim(self, name, namespace):
        self.calls.append(("read_pvc", namespace, name))
        raise FakeApiException(403, "Forbidden")

    def create_namespaced_persistent_volume_claim(self, namespace, body):
        self.calls.append(("create_pvc", namespace, body["metadata"]["name"]))
        raise FakeApiException(403, "Forbidden")

    def delete_namespaced_persistent_volume_claim(self, name, namespace, **_):
        self.calls.append(("delete_pvc", namespace, name))
        raise FakeApiException(403, "Forbidden")

    def pvc_calls(self):
        return [c for c in self.calls if c[0].endswith("_pvc")]

    def connect_get_namespaced_pod_exec(self, *a, **kw):
        raise AssertionError("exec must go through kubernetes.stream.stream")

    def stream(self, fn, name, namespace, *, command, container, stdin, binary, _preload_content, **_):
        assert fn == self.connect_get_namespaced_pod_exec and binary and not _preload_content
        assert container == "workspace"
        self._get(self.pods, namespace, name)
        ex = FakeExec(self, command, stdin)
        self.execs.append(ex)
        return ex


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    fake = FakeCluster(tmp_path.resolve())
    pkg = types.ModuleType("kubernetes")
    client = types.ModuleType("kubernetes.client")
    exceptions = types.ModuleType("kubernetes.client.exceptions")
    config = types.ModuleType("kubernetes.config")
    stream_mod = types.ModuleType("kubernetes.stream")
    exceptions.ApiException = FakeApiException
    client.CoreV1Api = lambda: fake
    client.exceptions = exceptions
    config.ConfigException = FakeConfigException

    def _no_incluster():
        raise FakeConfigException("not in a cluster")

    config.load_incluster_config = _no_incluster
    def _load_kube_config(config_file=None, context=None):
        fake.kubeconfig_file, fake.kube_context = config_file, context

    config.load_kube_config = _load_kube_config
    stream_mod.stream = fake.stream
    pkg.client, pkg.config, pkg.stream = client, config, stream_mod
    for name, mod in (("kubernetes", pkg), ("kubernetes.client", client), ("kubernetes.client.exceptions", exceptions),
                      ("kubernetes.config", config), ("kubernetes.stream", stream_mod)):
        monkeypatch.setitem(sys.modules, name, mod)
    yield fake
    for ex in fake.execs:
        if ex.proc.poll() is None:
            ex.proc.kill()


@pytest.fixture
def make_env(cluster):
    from tools.environments.kubernetes import KubernetesEnvironment
    envs = []

    def _make(**kw):
        kwargs = dict(namespace="agents", image="example.org/sandbox:1", profile="alice", task_id="session-1",
                      timeout=30)
        kwargs.update(kw)
        env = KubernetesEnvironment(**kwargs)
        envs.append(env)
        return env

    yield _make
    for env in envs:
        env.cleanup()


def test_exec_returns_stdout_and_real_exit_code(make_env):
    env = make_env()

    ok = env.execute("echo hello-from-pod")
    failed = env.execute("echo about-to-fail; exit 7")

    assert ok["returncode"] == 0
    assert "hello-from-pod" in ok["output"]
    assert failed["returncode"] == 7
    assert "about-to-fail" in failed["output"]


def test_cwd_persists_across_commands(make_env):
    env = make_env()

    env.execute("mkdir -p /workspace/x && cd /workspace/x")
    result = env.execute("pwd")

    assert result["returncode"] == 0
    assert result["output"].strip() == "/workspace/x"
    assert env.cwd == "/workspace/x"


def test_command_past_timeout_is_killed_and_reported(make_env, cluster):
    env = make_env()

    result = env.execute("sleep 60", timeout=2)

    assert result["returncode"] == 124
    assert result.get("hermes_timed_out") is True
    assert "timed out" in result["output"]
    command_exec = next(ex for ex in cluster.execs if len(ex.argv) > 6 and "sleep 60" in ex.argv[6])
    kill_exec = next(ex for ex in cluster.execs if "kill -KILL" in ex.argv[2])
    assert kill_exec.argv[4] == command_exec.argv[4]  # the kill targets this command's pidfile
    wait_until(lambda: command_exec.proc.poll() is not None, timeout=10, what="remote command tree killed")


def test_idle_pod_is_deleted_without_any_pvc_api_call(make_env, cluster):
    env = make_env(idle_timeout=1)
    pod_key = ("agents", env.pod_name)
    assert pod_key in cluster.pods

    wait_until(lambda: pod_key not in cluster.pods, timeout=15, what="idle pod reaped")

    again = env.execute("echo back")  # next use recreates the pod
    assert again["returncode"] == 0 and "back" in again["output"]
    assert again.get("environment_recreated") is True
    assert pod_key in cluster.pods

    env.cleanup()
    assert pod_key not in cluster.pods
    assert cluster.pvc_calls() == []


def test_profile_config_drives_namespace_image_resources_and_security(cluster, monkeypatch):
    from hermes_constants import get_hermes_home
    from tools.terminal_tool_backends import _create_environment

    get_hermes_home().mkdir(parents=True, exist_ok=True)
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump({"terminal": {"backend": "kubernetes", "kubernetes": {
        "namespace": "team-ns", "image": "registry.example/agent:7", "cpu": "2", "memory": "3Gi",
        "ephemeral_storage": "4Gi", "context": "prod-ctx", "kubeconfig": "/etc/owner/kubeconfig",
        "labels": {"team": "research"}, "node_selector": {"pool": "agents"},
        "tolerations": [{"key": "agents", "operator": "Exists", "effect": "NoSchedule"}]}}}))
    monkeypatch.setenv("HERMES_PROFILE_NAME", "Research_Bot")

    env = _create_environment("kubernetes", image="", cwd="/workspace", timeout=30, task_id="sess-9")
    try:
        pod = cluster.pods[("team-ns", env.pod_name)]
    finally:
        env.cleanup()

    assert env.pod_name.startswith("hermes-research-bot-") and len(env.pod_name) <= 63
    assert (cluster.kubeconfig_file, cluster.kube_context) == ("/etc/owner/kubeconfig", "prod-ctx")
    assert pod["metadata"]["labels"] == {"team": "research", "app.kubernetes.io/managed-by": "hermes",
                                         "hermes/profile": "research-bot"}
    assert "hermes/last-used" in pod["metadata"]["annotations"]
    spec = pod["spec"]
    (container,) = spec["containers"]
    assert container["image"] == "registry.example/agent:7"
    limits = {"cpu": "2", "memory": "3Gi", "ephemeral-storage": "4Gi"}
    assert container["resources"] == {"requests": limits, "limits": limits}
    assert spec["nodeSelector"] == {"pool": "agents"}
    assert spec["tolerations"] == [{"key": "agents", "operator": "Exists", "effect": "NoSchedule"}]
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True and spec["securityContext"]["runAsUser"] == 1000
    sc = container["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["runAsUser"] == 1000
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"] == {"drop": ["ALL"]}
    assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
    # No workspace_pvc configured: an emptyDir workspace, plus the pod's own HOME.
    assert spec["volumes"] == [{"name": "workspace", "emptyDir": {}}, {"name": "home", "emptyDir": {}}]
    assert container["volumeMounts"] == [{"name": "workspace", "mountPath": "/workspace"},
                                         {"name": "home", "mountPath": "/home/hermes"}]
    assert container["env"] == [{"name": "HOME", "value": "/home/hermes"}]
    assert cluster.pvc_calls() == []


def test_workspace_pvc_and_subpath_mount_existing_claim_without_pvc_api(make_env, cluster):
    env = make_env(workspace_pvc="assistants-efs", workspace_subpath="owner-42/assistant-7")

    spec = cluster.pods[("agents", env.pod_name)]["spec"]
    env.cleanup()

    assert spec["volumes"] == [{"name": "workspace", "persistentVolumeClaim": {"claimName": "assistants-efs"}},
                               {"name": "home", "emptyDir": {}}]
    assert spec["containers"][0]["volumeMounts"] == [
        {"name": "workspace", "mountPath": "/workspace", "subPath": "owner-42/assistant-7"},
        {"name": "home", "mountPath": "/home/hermes"}]
    assert cluster.pvc_calls() == []


def test_workspace_mount_path_and_home_stay_apart(make_env, cluster):
    """A shared claim mounted at a chosen path; HOME (and the ~/.hermes sync) never lands on it."""
    env = make_env(workspace_pvc="workspaces", workspace_subpath="ada", workspace_mount="/workspace/ada")

    spec = cluster.pods[("agents", env.pod_name)]["spec"]
    container = spec["containers"][0]
    env.cleanup()

    assert container["volumeMounts"][0] == {"name": "workspace", "mountPath": "/workspace/ada", "subPath": "ada"}
    assert container["workingDir"] == "/workspace/ada"
    assert container["env"] == [{"name": "HOME", "value": "/home/hermes"}]
    for bad in ("relative", "/", "/home/hermes"):
        with pytest.raises(ValueError, match="workspace_mount"):
            make_env(workspace_mount=bad)


def test_run_as_ids_land_in_pod_security_context(make_env, cluster):
    env = make_env(run_as_user=4242, run_as_group=4343, fs_group=4444)

    spec = cluster.pods[("agents", env.pod_name)]["spec"]

    assert spec["securityContext"] == {"runAsNonRoot": True, "runAsUser": 4242, "runAsGroup": 4343, "fsGroup": 4444,
                                       "seccompProfile": {"type": "RuntimeDefault"}}
    sc = spec["containers"][0]["securityContext"]
    assert (sc["runAsUser"], sc["runAsGroup"], sc["runAsNonRoot"]) == (4242, 4343, True)


def test_running_pod_is_reused_not_recreated(make_env, cluster):
    first = make_env()
    first.execute("echo marker > /workspace/state")  # fake pods share one local dir standing in for a claim
    pod_body = cluster.pods[("agents", first.pod_name)]
    first._closed.set()  # drop the object without deleting its pod, like a restarted Hermes process

    second = make_env()

    assert second.pod_name == first.pod_name
    assert cluster.pods[("agents", second.pod_name)] is pod_body
    assert [c for c in cluster.calls if c[0] == "create_pod"] == [("create_pod", "agents", first.pod_name)]
    assert second.execute("cat /workspace/state")["output"].strip() == "marker"


def test_missing_kubernetes_package_names_the_extra(monkeypatch):
    from tools.environments.kubernetes import KubernetesEnvironment

    monkeypatch.setitem(sys.modules, "kubernetes", None)  # import fails as if the extra were absent

    with pytest.raises(ImportError, match=r"'kubernetes' extra"):
        KubernetesEnvironment(namespace="agents", image="img", profile="alice")


def test_requirements_check_names_the_extra_when_package_missing(monkeypatch):
    from hermes_constants import get_hermes_home
    from tools.terminal_tool_backends import _REQUIREMENT_CHECKERS, terminal_backend_unavailable_reason

    get_hermes_home().mkdir(parents=True, exist_ok=True)
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(
        {"terminal": {"kubernetes": {"namespace": "agents", "image": "img"}}}))
    monkeypatch.setitem(sys.modules, "kubernetes", None)

    assert _REQUIREMENT_CHECKERS["kubernetes"]({}) is False
    assert "'kubernetes' extra" in terminal_backend_unavailable_reason()


def test_fetch_file_streams_over_exec_with_size_cap(make_env, tmp_path):
    from tools.environments.base import FileFetchError

    env = make_env()
    env.execute("printf 'artifact-bytes' > /workspace/out.bin && head -c 4096 /dev/zero > /workspace/big.bin")

    dest = tmp_path / "fetched.bin"
    env.fetch_file("/workspace/out.bin", dest, max_bytes=1024)
    assert dest.read_bytes() == b"artifact-bytes"
    with pytest.raises(FileFetchError, match="exceeds"):
        env.fetch_file("/workspace/big.bin", tmp_path / "big.bin", max_bytes=1024)


def test_image_pull_failure_surfaces_connection_error(make_env, cluster):
    from tools.environments.base import EnvironmentConnectionError

    cluster.fake_waiting = "ImagePullBackOff"

    with pytest.raises(EnvironmentConnectionError, match="ImagePullBackOff"):
        make_env()


def test_failed_pod_is_replaced(make_env, cluster):
    from tools.environments.kubernetes import pod_name

    name = pod_name("alice", "session-1")
    cluster.pods[("agents", name)] = {"metadata": {"name": name}, "fake_phase": "Failed"}

    env = make_env()

    assert ("delete_pod", "agents", name) in cluster.calls
    assert ("create_pod", "agents", name) in cluster.calls
    assert env.execute("echo fresh")["returncode"] == 0


def test_root_uid_is_refused(cluster):
    from tools.environments.kubernetes import KubernetesEnvironment

    with pytest.raises(ValueError, match="non-zero"):
        KubernetesEnvironment(namespace="agents", image="img", profile="alice", run_as_user=0)
    assert cluster.pods == {}


def test_without_explicit_kubeconfig_falls_back_to_default_kubeconfig(make_env, cluster):
    make_env(context="dev")  # fake: in-cluster config unavailable

    assert (cluster.kubeconfig_file, cluster.kube_context) == (None, "dev")


def test_kubeconfig_token_reaches_the_request_header_with_the_real_client(tmp_path):
    """Live 2026-10-30: kubernetes 36.0.0 loaded the token but sent no Authorization header (401)."""
    pytest.importorskip("kubernetes")
    from kubernetes import client

    from tools.environments import kubernetes as k8s_env

    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(json.dumps({
        "apiVersion": "v1", "kind": "Config", "current-context": "c",
        "clusters": [{"name": "c", "cluster": {"server": "https://k.invalid"}}],
        "users": [{"name": "u", "user": {"token": "t0k"}}],
        "contexts": [{"name": "c", "context": {"cluster": "c", "user": "u"}}],
    }))
    try:
        k8s_env._load_kubernetes("", kubeconfig=str(kubeconfig))
        auth = client.Configuration.get_default_copy().auth_settings().values()
        assert any(a["in"] == "header" and a["key"].lower() == "authorization" and a["value"] == "Bearer t0k"
                   for a in auth), list(auth)
    finally:
        client.Configuration.set_default(None)
