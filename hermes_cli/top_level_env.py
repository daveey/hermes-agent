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


_AGENT_ATTRIBUTION_KEYS = ("HERMES_AGENT", "AI_AGENT", "HERMES_DESKTOP", "HERMES_SUPERVISED_CHILD",
                           "HERMES_SESSION_ID")


def drop_agent_attribution(env: dict[str, str]) -> dict[str, str]:
    """``env`` without the agent-harness markers (main()'s advertisement, a backend's or service's
    identity), for launching the Desktop GUI: Electron is not an agent, and its terminal pane (a
    person's shell) copies Electron's env, so a CLI comment typed there must not read as an
    agent's (kanban_comment_writer). The backend Electron spawns re-advertises them. [daveey]"""
    for key in _AGENT_ATTRIBUTION_KEYS:
        env.pop(key, None)
    return env


def clean_launch_env(base: Optional[MutableMapping[str, str]] = None) -> dict[str, str]:
    """A copy of ``base`` (default ``os.environ``) safe to hand to a top-level app launch."""
    env = dict(os.environ if base is None else base)
    scrub_leaked_worker_env(env)
    return drop_agent_attribution(env)


def _first_positional(argv: Sequence[str]) -> str:
    return next((arg for arg in argv if arg and not arg.startswith("-")), "")


def scrub_for_top_level_launch(argv: Sequence[str]) -> list[str]:
    """Scrub ``os.environ`` when ``argv`` (``sys.argv[1:]``) launches a top-level surface.

    Also drops a finished run's identity on every launch, before the command gate,
    so chat and kanban do not inherit a dead worker. [daveey]
    """
    try:
        from hermes_cli.kanban_identity import drop_finished_kanban_identity
        drop_finished_kanban_identity()
    except Exception:  # health: allow BLE001 -- a board-read failure must not abort startup
        import logging
        logging.getLogger(__name__).debug("finished-run identity drop failed", exc_info=True)
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


_TERMINAL_STATUSES = frozenset({"done", "archived"})


def _bound_pid(conn, task_id: str) -> Optional[int]:
    """The interpreter the owner bound to the task's current run (``worker_bound`` event).

    A managed worker runs in ``agent.managed_worker`` under the gateway, a sibling of the
    ``kanban_worker_client`` spawn pid ``task_runs.worker_pid`` records, so its shells descend
    from this pid and never from that one (t_01a18b6ffd5e33c1). [daveey]
    """
    import json
    import sqlite3

    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'worker_bound' AND run_id = "
            "(SELECT max(id) FROM task_runs WHERE task_id = ?) ORDER BY id DESC LIMIT 1",
            (task_id, task_id)).fetchone()
    except sqlite3.Error:
        return None
    try:
        pid = int((json.loads(row[0]) or {}).get("pid") or 0) if row and row[0] else 0
    except (TypeError, ValueError, AttributeError):
        return None
    return pid or None


def _task_state(db: Path, task_id: str) -> Optional[tuple[str, Optional[int], Optional[int]]]:
    """``(status, worker_pid, bound_pid)`` of ``task_id``; ``("", None, None)`` when absent; ``None``
    when unreadable. ``bound_pid`` is the latest run's ``worker_bound`` interpreter, if any."""
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
            if row is not None and not row[1]:
                # A live worker whose card was parked (kanban_block -> todo/blocked) or finished
                # (kanban_complete -> done) has its pid cleared on the card but retained on its latest
                # run: that is still the worker, not a leak.
                try:
                    run = conn.execute("SELECT worker_pid FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                                       (task_id,)).fetchone()
                except sqlite3.Error:
                    run = None
                if run is not None and run[0]:
                    row = (row[0], run[0])
            bound = _bound_pid(conn, task_id) if row is not None else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return ("", None, None)
    return (str(row[0] or ""), int(row[1]) if row[1] else None, bound)


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


def _descends_from_live_agent(env: Mapping[str, str], bound_pid: Optional[int]) -> bool:
    """Whether this process descends from a live agent of the lineage other than the spawn pid:
    the run's ``worker_bound`` interpreter, or the ``HERMES_DELEGATED_CHILD_OWNER`` process that
    fenced the lineage (that same managed worker, named even when no bind row is readable). [daveey]"""
    from agent.delegation_context import DELEGATED_CHILD_OWNER_ENV

    owner = (env.get(DELEGATED_CHILD_OWNER_ENV) or "").strip().partition(":")[0]
    candidates = [bound_pid] + ([int(owner)] if owner.isdigit() else [])
    return any(pid and pid > 1 and _pid_alive(pid) and _is_ancestor(pid) for pid in candidates)


def leaked_worker_env_diagnosis(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Why this process's ``HERMES_DELEGATED_CHILD_CONTEXT`` looks leaked, or ``None``.

    A real delegate child (or a worker's tool subprocess) descends from a live worker whose card is
    ``running``; one whose card is already done/archived gets a "card is finished, use
    kanban_comment" hint instead of the leak/Desktop advice. A marker whose worker card is
    finished, whose worker pid is gone, or which this
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
    status, worker_pid, bound_pid = state
    if not status:
        reason = f"its worker card {tid} is not on the board"
    elif status in _TERMINAL_STATUSES:
        if _descends_from_live_agent(env, bound_pid) or (
                worker_pid and worker_pid > 1 and _pid_alive(worker_pid) and _is_ancestor(worker_pid)):
            # The worker's own shell after kanban_complete: the env is legitimately its own, the card
            # is just finished. No Desktop is involved (t_01a198dc526bff6e). [daveey]
            return (f"your Kanban card {tid} is {status}: board writes from its worker shell are fenced. "
                    "Use kanban_comment on the card you mean (or ask an operator chat), not the CLI.")
        reason = f"its worker card {tid} is {status}"
    elif status != "running" and not (worker_pid or bound_pid):
        reason = f"its worker card {tid} is {status}"
    elif _descends_from_live_agent(env, bound_pid):
        return None  # a live managed worker / delegation owner is this process's ancestor
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
