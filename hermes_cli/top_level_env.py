"""Strip a kanban worker's / delegate child's identity env from top-level launches. [daveey]

A dispatcher-spawned worker carries ``HERMES_KANBAN_TASK``/``..._WORKSPACE``/``..._DB`` pins and,
in delegate children, ``HERMES_DELEGATED_CHILD_CONTEXT``. When a worker ran ``hermes update`` the
update reopened Hermes Desktop with ``/usr/bin/open``, which hands the caller's environment to the
app: the Desktop (and every backend and terminal it spawned) then believed it was that worker's
delegate child, so every chat's kanban tools refused ("delegate_task child contexts cannot mutate
Kanban") and the board was pinned to the worker's DB (2026-10-21, Hermes.app pid 59331).

A Desktop, ``serve``/``dashboard`` backend or messaging gateway is never a worker. Each strips
these keys at launch, and code that reopens Desktop passes :func:`clean_launch_env`.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Mapping, MutableMapping, Optional, Sequence

# The key family and the marker rule live in agent/delegation_context.py so they can't drift.
from agent.delegation_context import (
    AGENT_SCOPE_MARKER_KEYS as WORKER_MARKER_KEYS,
    DELEGATED_CHILD_ENV_MARKER,
    strip_agent_scope_env,
)

# First positional argv of a top-level surface (never a worker).
TOP_LEVEL_COMMANDS = frozenset({"serve", "dashboard", "gui", "desktop", "gateway"})


def scrub_leaked_worker_env(environ: Optional[MutableMapping[str, str]] = None) -> list[str]:
    """Remove the agent-scope env family (worker/delegate/session/cron/RPC identity) from
    ``environ`` (default ``os.environ``) when any worker marker is present. Returns the removed
    key names (empty when nothing leaked)."""
    return strip_agent_scope_env(os.environ if environ is None else environ)


def clean_launch_env(base: Optional[MutableMapping[str, str]] = None) -> dict[str, str]:
    """A copy of ``base`` (default ``os.environ``) safe to hand to a top-level app launch."""
    env = dict(os.environ if base is None else base)
    scrub_leaked_worker_env(env)
    return env


def _first_positional(argv: Sequence[str]) -> str:
    return next((arg for arg in argv if arg and not arg.startswith("-")), "")


def scrub_for_top_level_launch(argv: Sequence[str]) -> list[str]:
    """Scrub ``os.environ`` when ``argv`` (``sys.argv[1:]``) launches a top-level surface."""
    if _first_positional(argv) not in TOP_LEVEL_COMMANDS:
        return []
    # Name the dead lineage while the pins still exist (the scrub removes them). [daveey]
    diagnosis = leaked_worker_env_diagnosis() if os.environ.get(DELEGATED_CHILD_ENV_MARKER) else None
    removed = scrub_leaked_worker_env()
    if removed:
        import logging

        logging.getLogger(__name__).warning(
            "hermes %s: dropped inherited agent-scope env %s (launched from a kanban worker/delegate child)%s",
            _first_positional(argv), ", ".join(removed), f"; {diagnosis}" if diagnosis else "")
    return removed


# ── detection: a delegation marker with no live parent agent ─────────────────────────────────

_TASK_ID_RE = re.compile(r"^t_[0-9a-f]{6,}$")


def _lineage_task_id(env: Mapping[str, str]) -> str:
    """The worker task this env descends from: ``HERMES_KANBAN_TASK``, else the workspace dir name
    (a delegate child's subprocess env keeps the workspace but drops the task id)."""
    tid = (env.get("HERMES_KANBAN_TASK") or "").strip()
    if tid:
        return tid
    name = Path((env.get("HERMES_KANBAN_WORKSPACE") or "").strip() or "/").name
    return name if _TASK_ID_RE.match(name) else ""


def _board_db(env: Mapping[str, str]) -> Optional[Path]:
    pinned = (env.get("HERMES_KANBAN_DB") or "").strip()
    if pinned:
        return Path(pinned).expanduser()
    try:
        from hermes_cli.kanban_db import kanban_db_path
        return kanban_db_path()
    except Exception:
        return None


def _task_state(db: Path, task_id: str) -> Optional[tuple[str, Optional[int]]]:
    """``(status, worker_pid)`` of ``task_id``; ``("", None)`` when absent; ``None`` when unreadable."""
    import sqlite3

    if not db.is_file():
        return None
    try:
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
            conn.execute("select 1 from sqlite_master limit 1")
        except sqlite3.OperationalError:  # WAL db with no -wal/-shm present: rw + query_only
            conn = sqlite3.connect(f"file:{db}?mode=rw", uri=True, timeout=2)
            conn.execute("PRAGMA query_only=ON")
        try:
            row = conn.execute("SELECT status, worker_pid FROM tasks WHERE id = ?", (task_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return ("", None)
    return (str(row[0] or ""), int(row[1]) if row[1] else None)


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    try:
        os.kill(pid, 0)  # windows-footgun: ok (psutil-less POSIX fallback)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _parent_pid(pid: int) -> Optional[int]:
    try:
        import psutil
        return int(psutil.Process(pid).ppid())
    except ImportError:
        pass
    except Exception:
        return None
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=5, check=False).stdout.strip()
        return int(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _is_ancestor(pid: int, start: Optional[int] = None) -> bool:
    current = os.getpid() if start is None else start
    for _ in range(64):
        if current == pid:
            return True
        parent = _parent_pid(current)
        if not parent or parent == current or parent <= 1:
            return pid == parent
        current = parent
    return False


def leaked_worker_env_diagnosis(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Why this process's ``HERMES_DELEGATED_CHILD_CONTEXT`` looks leaked, or ``None``.

    A real delegate child (or a worker's tool subprocess) descends from a live worker whose card is
    ``running``. A marker whose worker card is finished, whose worker pid is gone, or which this
    process does not descend from was inherited by a launch from inside a worker. Conservative:
    ``None`` whenever the lineage task can't be identified or the board can't be read.
    """
    env = os.environ if environ is None else environ
    if not env.get(DELEGATED_CHILD_ENV_MARKER):
        return None
    tid = _lineage_task_id(env)
    if not tid:
        return None
    db = _board_db(env)
    state = _task_state(db, tid) if db is not None else None
    if state is None:
        return None
    status, worker_pid = state
    if not status:
        reason = f"its worker card {tid} is not on the board"
    elif status != "running":
        reason = f"its worker card {tid} is {status}"
    elif worker_pid and not _pid_alive(worker_pid):
        reason = f"{tid}'s worker pid {worker_pid} is gone"
    elif worker_pid and not _is_ancestor(worker_pid):
        reason = f"pid {os.getpid()} does not descend from {tid}'s worker pid {worker_pid}"
    else:
        return None
    return (f"leaked worker env: this process inherited {DELEGATED_CHILD_ENV_MARKER} from kanban card {tid} "
            f"but no parent agent is alive ({reason}). It is not a real delegate_task child: it was "
            "launched from inside a worker (e.g. a worker's `hermes update` reopening Hermes Desktop). "
            "Quit and reopen it (Hermes Desktop: Cmd-Q, then open the app) to restore Kanban writes.")


def delegated_child_refusal(base: str) -> str:
    """``base`` refusal text, plus the leak diagnosis when the fence comes from a leaked env."""
    try:
        diagnosis = leaked_worker_env_diagnosis()
    except Exception:
        diagnosis = None
    return f"{base}. {diagnosis}" if diagnosis else base
