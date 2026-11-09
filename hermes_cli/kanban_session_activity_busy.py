"""A busy board degrades only itself in the sidebar activity reads. [daveey]

kanban-359833be53: while another thread held one board's lifecycle lock, the count read aborted as a whole and the
sidebar marked EVERY chat row ``kanban_unknown``; ``session_tasks`` failed the same way. Like the content probe and the
origin-work snapshot save, a busy board now means "degrade, retry next time": it serves its last good answer, logs
one WARNING line (no traceback, so self-heal sees a rate rather than a crash) and is read again on the next pass. A
board never read successfully before still raises: unknown, never "no work". A corrupt or missing board is not busy
and still raises.

State is per process; ``kanban_session_activity`` resets it with its caches.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from hermes_cli.kanban_generation import is_busy
from hermes_cli.kanban_session_activity_revalidate import is_busy_key

_log = logging.getLogger("hermes_cli.kanban_session_activity")

# .reused: a busy board's last good counts stood in for a read in this thread's recompute. Such counts must not be
# revalidated past the TTL on an unchanged fingerprint: only a real read of that board may extend them.
reuse_sink = threading.local()

_tasks_memo: dict[tuple, list[dict[str, Any]]] = {}  # last good per-board task list, by (board, ids, statuses)
_tasks_lock = threading.Lock()
TASKS_MEMO_MAX = 512


def begin_recompute() -> None:
    reuse_sink.reused = False


def reused() -> bool:
    return bool(getattr(reuse_sink, "reused", False))


def board_reads(path: Path, key: tuple, hit: tuple | None, parked: bool, stalled: bool,
                read: Callable[..., tuple[dict[str, Any], bool]]) -> tuple[dict[str, Any], bool, bool]:
    """``(reads, complete, fresh)`` for one board whose memo row did not match ``key``. A busy board with a usable
    memo row ``hit`` returns its last good reads (``fresh`` False: keep that row, so the next pass re-reads); a busy
    memo key (``is_busy_key``) already spent this board's busy budget, so it never waits out a second one."""
    usable = hit is not None and (not parked or "parked" in hit[1]) and (not stalled or "stall_rows" in hit[1])
    try:
        if is_busy_key(key):
            raise key[1].error
        reads, complete = read(path, parked=parked, stalled=stalled)
    except sqlite3.OperationalError as exc:
        if hit is None or not usable or not is_busy(exc):
            raise
        _log.warning("kanban board %s busy; reused last counts", path)
        reuse_sink.reused = True
        return hit[1], False, False
    return reads, complete, True


def session_tasks(path: Path, ids: list[str], statuses: tuple[str, ...],
                  read: Callable[[Path, list[str], tuple[str, ...]], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """``read(path, ids, statuses)``, or on a busy board its last good list for the same query, every row ``stale``."""
    key = (str(path), tuple(ids), tuple(statuses))
    try:
        tasks = read(path, ids, statuses)
    except sqlite3.OperationalError as exc:
        with _tasks_lock:
            last = _tasks_memo.get(key)
        if last is None or not is_busy(exc):
            raise
        _log.warning("kanban board %s busy; reused last counts", path)
        return [{**t, "stale": True} for t in last]
    with _tasks_lock:
        _tasks_memo.pop(key, None)
        _tasks_memo[key] = tasks
        while len(_tasks_memo) > TASKS_MEMO_MAX:
            _tasks_memo.pop(next(iter(_tasks_memo)))
    return tasks


def reset() -> None:
    with _tasks_lock:
        _tasks_memo.clear()
