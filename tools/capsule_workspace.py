"""Task-only capsule workspace capability for a dedicated Kanban worker process.

The trusted host controller (the observer, after its authenticated IPC handshake) calls
:func:`bind_worker_workspace` once. From then on every execution environment this process creates
or keeps cached must be a runsc Docker sandbox whose only host mount is that exact directory.
The binding is host-Python only: no tool, config key or env var can create, widen or restore it.
Env vars (``HERMES_KANBAN_TASK`` / ``HERMES_KANBAN_RUN_ID``) are cross-checked, never trusted.

Any failure of the binding itself (closed, liveness false, task/run/profile/PID/workspace drift)
latches a tombstone: the process can never run tools again, bound or ordinary. A request that
drifts from the capsule policy (wrong backend, extra mounts, credential forwarding) is refused
without revoking.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Optional

CAPSULE_CWD = "/workspace"
RUNSC_EXTRA_ARGS = ("--runtime=runsc",)
# Characters that would change the meaning of a ``-v host:container`` spec or a log line.
_FORBIDDEN_PATH_CHARS = (":", ",", "\n", "\r", "\0")


class CapsuleWorkspaceError(RuntimeError):
    """The capsule capability is missing, revoked, stale, or the request would widen it."""


class WorkspaceBinding:
    """Process-local handle for one bound capsule; deliberately not copyable or serializable."""

    __slots__ = ("_identity", "_is_live", "_pid", "_profile_home", "_run_id", "_task_id", "_workspace")

    def __init__(self, *, task_id, run_id, profile_home, workspace, identity, is_live):
        self._task_id, self._run_id = task_id, run_id
        self._profile_home, self._workspace = profile_home, workspace
        self._identity, self._pid, self._is_live = identity, os.getpid(), is_live

    task_id = property(lambda self: self._task_id)
    run_id = property(lambda self: self._run_id)
    profile_home = property(lambda self: self._profile_home)
    workspace = property(lambda self: self._workspace)

    def close(self) -> None:
        """Revoke for the rest of this process (fail-closed tombstone)."""
        _STATE.tombstone(self, "binding closed")

    def __reduce_ex__(self, protocol):
        raise TypeError("WorkspaceBinding is process-local and cannot be serialized")

    def __copy__(self):
        raise TypeError("WorkspaceBinding cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("WorkspaceBinding cannot be copied")

    def __repr__(self) -> str:
        return f"<WorkspaceBinding task={self._task_id} run={self._run_id} workspace={self._workspace}>"


class _ProcessState:
    def __init__(self):
        self.lock = threading.Lock()
        self.binding: Optional[WorkspaceBinding] = None
        self.dead_reason: Optional[str] = None

    def tombstone(self, binding: WorkspaceBinding, reason: str) -> None:
        with self.lock:
            if binding is self.binding and self.dead_reason is None:
                self.dead_reason = reason


_STATE = _ProcessState()


def _canonical_dir(path: Any, what: str) -> str:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise CapsuleWorkspaceError(f"{what} must be an absolute path string: {path!r}")
    if any(ch in path for ch in _FORBIDDEN_PATH_CHARS):
        raise CapsuleWorkspaceError(f"{what} contains a character Docker mount specs cannot carry: {path!r}")
    if os.path.realpath(path) != path or os.path.islink(path) or not os.path.isdir(path):
        raise CapsuleWorkspaceError(f"{what} must be an existing canonical directory: {path!r}")
    return path


def _dir_identity(path: str) -> tuple[int, int]:
    st = os.lstat(path)
    return st.st_dev, st.st_ino


def _active_profile_home() -> str:
    from hermes_constants import get_hermes_home
    return os.path.realpath(str(get_hermes_home()))


def _check_worker_identity(task_id: str, run_id: str) -> None:
    for var, want in (("HERMES_KANBAN_TASK", task_id), ("HERMES_KANBAN_RUN_ID", run_id)):
        if os.environ.get(var, "") != want:
            raise CapsuleWorkspaceError(f"{var} does not match the bound capsule")


def _is_true(is_live: Callable[[], Any]) -> bool:
    try:
        return is_live() is True
    except Exception:  # any liveness failure means not live: fail closed
        logging.getLogger(__name__).debug("capsule liveness check raised; not live", exc_info=True)
        return False


def bind_worker_workspace(task_id: str, run_id: str, profile_home: str, workspace: str,
                          is_live: Callable[[], bool]) -> WorkspaceBinding:
    """Install this process's single capsule binding (host controller only).

    ``task_id``/``run_id`` must equal ``HERMES_KANBAN_TASK``/``HERMES_KANBAN_RUN_ID``;
    ``profile_home`` must be the canonical active Hermes home; ``workspace`` an existing canonical
    directory (its inode is pinned); ``is_live()`` must return ``True`` now and on every later use.
    A second call in the same process is refused, even after ``close()``."""
    for name, value in (("task_id", task_id), ("run_id", run_id)):
        if not isinstance(value, str) or not value:
            raise CapsuleWorkspaceError(f"{name} must be a non-empty string")
    _check_worker_identity(task_id, run_id)
    home = _canonical_dir(profile_home, "profile_home")
    if home != _active_profile_home():
        raise CapsuleWorkspaceError("profile_home is not the active Hermes home")
    ws = _canonical_dir(workspace, "workspace")
    if not callable(is_live) or not _is_true(is_live):
        raise CapsuleWorkspaceError("is_live must be a callable that returns True")
    binding = WorkspaceBinding(task_id=task_id, run_id=run_id, profile_home=home, workspace=ws,
                               identity=_dir_identity(ws), is_live=is_live)
    with _STATE.lock:
        if _STATE.binding is not None:
            raise CapsuleWorkspaceError("this worker process already had a capsule binding")
        _STATE.binding = binding
    return binding


def _binding_problem(b: WorkspaceBinding) -> Optional[str]:
    if os.getpid() != b._pid:
        return "process forked since bind"
    try:
        _check_worker_identity(b._task_id, b._run_id)
    except CapsuleWorkspaceError as exc:
        return str(exc)
    if _active_profile_home() != b._profile_home:
        return "active profile changed"
    try:
        if (os.path.islink(b._workspace) or os.path.realpath(b._workspace) != b._workspace
                or not os.path.isdir(b._workspace) or _dir_identity(b._workspace) != b._identity):
            return "workspace path was replaced"
    except OSError:
        return "workspace is gone"
    if not _is_true(b._is_live):
        return "observer reports the run is no longer live"
    return None


def current_binding() -> Optional[WorkspaceBinding]:
    """The valid binding, ``None`` when this process was never bound; raises (and latches) otherwise."""
    with _STATE.lock:
        binding, dead = _STATE.binding, _STATE.dead_reason
    if binding is None:
        return None
    if dead is None:
        dead = _binding_problem(binding)
        if dead is not None:
            _STATE.tombstone(binding, dead)
    if dead is not None:
        raise CapsuleWorkspaceError(f"capsule workspace revoked: {dead}")
    return binding


def bound() -> bool:
    """True once this process installed a capsule binding (live, revoked or closed: it never reverts).

    Model-facing defaults (the container cwd, the worker's workspace contract) read this, never env or
    config: a bound worker's only workspace is :data:`CAPSULE_CWD`, and a revoked one runs nothing at all,
    so it never needs a host fallback. Does not validate or latch; execution still goes through
    :func:`current_binding` / :func:`require_binding`."""
    with _STATE.lock:
        return _STATE.binding is not None


def inside(path: Any) -> bool:
    """``path`` is :data:`CAPSULE_CWD` or below it (a container path, compared lexically)."""
    if not isinstance(path, str) or "\0" in path:
        return False
    norm = os.path.normpath(path)
    return norm == CAPSULE_CWD or norm.startswith(CAPSULE_CWD + "/")


def require_binding(handle: Optional[WorkspaceBinding]) -> None:
    """Gate for an environment about to run anything: ``handle`` must be the live binding, and an
    environment without a handle may only run when this process was never bound."""
    if current_binding() is not handle:
        raise CapsuleWorkspaceError("environment does not hold this process's capsule binding")


def docker_kwargs(binding: WorkspaceBinding, env_type: str, cc: dict) -> dict:
    """Exact ``DockerEnvironment`` kwargs for the bound capsule, or refuse a drifted request."""
    if env_type != "docker":
        raise CapsuleWorkspaceError(f"capsule workers run only on the docker backend, not {env_type!r}")
    problems = [key for key, ok in (
        ("docker_extra_args", cc.get("docker_extra_args") == list(RUNSC_EXTRA_ARGS)),
        ("container_persistent", cc.get("container_persistent", True) is False),
        ("docker_volumes", not cc.get("docker_volumes")),
        ("docker_env", not cc.get("docker_env")),
        ("docker_forward_env", not cc.get("docker_forward_env")),
        ("docker_shared_container_key", not cc.get("docker_shared_container_key")),
        ("docker_snap_compat", not cc.get("docker_snap_compat")),
        ("docker_mount_cwd_to_workspace", not cc.get("docker_mount_cwd_to_workspace")),
    ) if not ok]
    if problems:
        raise CapsuleWorkspaceError(f"terminal config widens the capsule sandbox: {', '.join(problems)}")
    return dict(
        cwd=CAPSULE_CWD, host_cwd=binding.workspace, auto_mount_cwd=True, capsule=binding,
        cpu=cc.get("container_cpu", 1), memory=cc.get("container_memory", 5120),
        disk=cc.get("container_disk", 51200), persistent_filesystem=False, volumes=[], env={},
        forward_env=[], extra_args=list(RUNSC_EXTRA_ARGS), persist_across_processes=False,
        shared_container_key="", snap_compat=False, network=cc.get("docker_network", True),
        run_as_host_user=cc.get("docker_run_as_host_user", False), shm_size=cc.get("docker_shm_size", "1g"),
        image_pinned=cc.get("docker_image_pinned", False))


def check_docker_request(binding: WorkspaceBinding, **requested) -> None:
    """Defence in depth inside ``DockerEnvironment``: the constructor kwargs must be exactly the
    capsule shape, whoever built them."""
    expected = {"cwd": CAPSULE_CWD, "host_cwd": binding.workspace, "auto_mount_cwd": True,
                "persistent_filesystem": False, "persist_across_processes": False,
                "shared_container_key": "", "snap_compat": False, "extra_args": list(RUNSC_EXTRA_ARGS)}
    wrong = [k for k, v in expected.items() if requested.get(k) != v]
    wrong += [k for k in ("volumes", "env", "forward_env") if requested.get(k)]
    if wrong:
        raise CapsuleWorkspaceError(f"capsule docker request is widened: {', '.join(sorted(wrong))}")
