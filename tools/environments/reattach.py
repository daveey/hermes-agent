"""Re-open a sandbox after a host restart, just far enough to watch a background job. [daveey]

``ProcessRegistry.spawn_via_env`` runs a background command inside a sandbox with a wrapper that
writes ``hermes_bg_<id>.{log,pid,exit}`` into the sandbox temp dir, then polls those files through
the environment handle. A gateway restart loses that handle, but a persistent sandbox (a Docker
container with ``container_persistent: true``) keeps running, and so does the job. The spawn
records ``env.reattach_handle()`` in the process checkpoint; on recovery,
:func:`reattach_environment` turns it back into an object with the ``execute()`` the poller needs.

It is deliberately minimal: it never creates, starts or recreates a sandbox (a fresh container
would not hold the job, and the poller would invent an exit). ``None`` means "cannot re-attach":
the caller then tells the owning session its watcher was lost.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from datetime import UTC, datetime
from typing import Any, Optional

logger = logging.getLogger("tools.process_registry")

_GONE_PATTERNS = ("No such container", "is not running", "no such container")
# Printed after the command so a docker-level failure (daemon unreachable, paused container) is never
# mistaken for the command's own output: the poller would read it as "the job exited".
_RC_MARKER = "__HERMES_REATTACH_RC="


class SandboxGoneError(RuntimeError):
    """The re-attached sandbox disappeared; the poller then marks the job lost, never exited."""


class ReattachedDockerSandbox:
    """``docker exec`` into an already-running container. No create/start/recreate."""

    def __init__(self, docker_exe: str, container_id: str, started_at: Optional[float] = None):
        self.docker_exe = docker_exe
        self.container_id = container_id
        # When the container (re)started; a job that started before it cannot be running in it.
        self.started_at = started_at

    def get_temp_dir(self) -> str:
        return "/tmp"  # no-tmp: ok — the in-container temp dir spawn_via_env used

    def reattach_handle(self) -> dict[str, Any]:
        return {"backend": "docker", "docker_exe": self.docker_exe, "container_id": self.container_id}

    def execute(self, command: str, cwd: str = "", timeout: int = 10, **_kwargs) -> dict:
        script = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        # A subshell, so an `exit` in the command still reaches the marker line.
        script = f"( {script}\n)\nprintf '\\n{_RC_MARKER}%s\\n' \"$?\""
        try:
            res = subprocess.run([self.docker_exe, "exec", self.container_id, "bash", "-c", script],
                                 capture_output=True, text=True, timeout=timeout, check=False,
                                 stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return {"output": "", "returncode": 124}
        stdout, stderr = res.stdout or "", res.stderr or ""
        body, sep, tail = stdout.rpartition(f"\n{_RC_MARKER}")
        if not sep:
            if any(p in stderr for p in _GONE_PATTERNS):
                raise SandboxGoneError(f"container {self.container_id[:12]} is gone: {stderr.strip()[:200]}")
            # The command never ran (docker-level failure): an empty result makes the poller skip this tick.
            logger.debug("re-attached docker exec failed (rc=%s): %s", res.returncode, stderr.strip()[:200])
            return {"output": "", "returncode": 125}
        try:
            rc = int(tail.strip().splitlines()[0])
        except (ValueError, IndexError):
            rc = res.returncode
        return {"output": body + ("\n" if body and not body.endswith("\n") else "") + stderr
                if stderr else body, "returncode": rc}

    def cleanup(self, **_kwargs) -> None:
        """Never stops the container: it belongs to the profile, not to this watcher."""


def _docker_container_state(docker_exe: str, container_id: str) -> tuple[bool, Optional[float]]:
    """(running, started_at epoch or None) for *container_id*."""
    try:
        res = subprocess.run(
            [docker_exe, "inspect", "-f", "{{.State.Running}} {{.State.StartedAt}}", container_id],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("Sandbox re-attach: docker inspect %s failed: %s", container_id[:12], exc)
        return False, None
    if res.returncode != 0:
        return False, None
    running, _, started = res.stdout.strip().partition(" ")
    started_at = None
    try:
        started_at = datetime.strptime(started.strip()[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=UTC).timestamp()
    except ValueError:
        pass
    return running == "true", started_at


def reattach_environment(handle: dict[str, Any]) -> Optional[Any]:
    """An ``execute()``-capable handle on the still-running sandbox *handle* names, else None."""
    if not isinstance(handle, dict):
        return None
    backend = handle.get("backend")
    if backend == "docker":
        exe, cid = str(handle.get("docker_exe") or "docker"), str(handle.get("container_id") or "")
        if not cid:
            return None
        running, started_at = _docker_container_state(exe, cid)
        return ReattachedDockerSandbox(exe, cid, started_at) if running else None
    return None  # other backends (ssh, modal, daytona, …) cannot be re-attached yet: lost-watcher notice


def env_reattach_handle(env: Any) -> dict[str, Any]:
    """``env.reattach_handle()`` or ``{}``; never raises (a spawn must not fail on bookkeeping)."""
    getter = getattr(env, "reattach_handle", None)
    if not callable(getter):
        return {}
    try:
        handle = getter()
    except Exception:
        logger.debug("reattach_handle() failed", exc_info=True)
        return {}
    return dict(handle) if isinstance(handle, dict) else {}
