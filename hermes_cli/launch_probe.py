"""Bounded, passive dependency-environment probe for launchers.

``probe(root)`` asks an isolated worker (``sys.executable -I -S``) whether the
environment recorded for ``root`` is current, or (``operation='runtime'``) to
activate it and report the resulting PATH. It never installs or repairs.

Contract (stdlib only, so a bootstrap interpreter can import it):

* ``{"schema": 1, "state": "current" | "stale"}`` for ``currency``.
* ``{"schema": 1, "state": "runtime", "problems": [str], "path": str}`` for ``runtime``.
* ``{"schema": 1, "state": "error", "detail": str}`` when the worker ran and failed.
  Permission failures with a denied path also carry ``permission_error`` with
  ``errno``, ``strerror`` and ``filename`` so launchers retain actionable diagnostics.
* ``None`` when the probe is unavailable, times out or answers malformed data.

The worker imports PM from the *implementation* checkout that contains this
file (``SOURCE_ROOT``), never from ``root``, which is only the project the
question is about (it may be a fixture or another checkout). One monotonic
deadline covers worker start-up, PM work and reading the answer. On POSIX the
worker runs in its own process group, and that group is SIGKILLed on every
exit, success included, so a descendant holding the stdout pipe can neither
hang us nor outlive the probe.

Windows: not implemented. ``probe`` returns ``None`` there, so callers take
their normal "probe unavailable" fallback.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from hermes_cli._startup_fast import project_root_str

SCHEMA = 1
SOURCE_ROOT = Path(project_root_str())
DEFAULT_TIMEOUT = 1.0  # seconds, end to end: worker start, PM work and the answer
_MAX_RESPONSE = 1 << 16
_REAP_SECONDS = 1.0

_WORKER = r"""
import json, os, sys
from pathlib import Path
source, root, operation = sys.argv[1:4]
root = Path(root)
sys.path.insert(0, source)
out = os.fdopen(os.dup(1), "w")
sys.stdout = sys.stderr  # imports must not pollute the response channel
try:
    if operation == "currency":
        import pm
        reply = {"schema": 1, "state": "current" if pm.venv_is_current(project_root=root) else "stale"}
    else:
        from pm.environments import activate_dependencies
        activate_dependencies(root)  # before any third-party import
        import pm
        problems = pm.activate()
        reply = {"schema": 1, "state": "runtime", "problems": [str(p) for p in problems],
                 "path": os.environ.get("PATH", "")}
except BaseException as exc:
    reply = {"schema": 1, "state": "error", "detail": f"{type(exc).__name__}: {exc}"}
    if isinstance(exc, PermissionError) and exc.filename is not None:
        reply["permission_error"] = {"errno": exc.errno, "strerror": exc.strerror, "filename": str(exc.filename)}
out.write(json.dumps(reply) + "\n")
out.flush()
"""


def _valid(operation: str, reply: Any) -> bool:
    if not isinstance(reply, dict) or reply.get("schema") != SCHEMA:
        return False
    state = reply.get("state")
    if state == "error":
        if not isinstance(reply.get("detail"), str):
            return False
        if set(reply) == {"schema", "state", "detail"}:
            return True
        error = reply.get("permission_error")
        return (set(reply) == {"schema", "state", "detail", "permission_error"}
                and isinstance(error, dict) and set(error) == {"errno", "strerror", "filename"}
                and (error["errno"] is None or type(error["errno"]) is int)
                and (error["strerror"] is None or isinstance(error["strerror"], str))
                and isinstance(error["filename"], str))
    if operation == "currency":
        return state in ("current", "stale") and set(reply) == {"schema", "state"}
    problems = reply.get("problems")
    return (state == "runtime" and isinstance(problems, list)
            and all(isinstance(p, str) for p in problems) and isinstance(reply.get("path"), str)
            and set(reply) == {"schema", "state", "problems", "path"})


def _read_line(proc: subprocess.Popen, deadline: float) -> bytes | None:
    """First stdout line, or None at the deadline/EOF/oversize. Never waits for pipe EOF
    once a line arrived: a descendant may keep the pipe open forever."""
    assert proc.stdout is not None
    buffer = b""
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while b"\n" not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return None
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                return None
            buffer += chunk
            if len(buffer) > _MAX_RESPONSE:
                return None
    return buffer.split(b"\n", 1)[0]


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # windows-footgun: ok — probe() runs only on POSIX
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def probe(root: str | os.PathLike[str], operation: str = "currency",
          timeout: float | None = None) -> dict[str, Any] | None:
    """Run one passive probe for ``root``; see the module docstring for the result shapes."""
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    if os.name != "posix" or operation not in ("currency", "runtime") or not timeout > 0:
        return None
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _WORKER, str(SOURCE_ROOT), os.fspath(root), operation],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            # detached-spawn: ok — awaited under one deadline, then its whole group is SIGKILLed on every exit
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        line = _read_line(proc, deadline)
        if line is None:
            return None
        try:
            reply = json.loads(line)
        except ValueError:
            return None
        return reply if _valid(operation, reply) else None
    finally:
        _kill_group(proc)
        assert proc.stdout is not None
        proc.stdout.close()
