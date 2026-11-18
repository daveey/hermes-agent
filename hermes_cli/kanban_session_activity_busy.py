"""A busy board degrades only itself in the sidebar activity reads. [daveey]

kanban-359833be53: while another thread held one board's lifecycle lock, the count read aborted as a whole and the
sidebar marked EVERY chat row ``kanban_unknown``; ``session_tasks`` failed the same way. Like the content probe and the
origin-work snapshot save, a busy board now means "degrade, retry next time": it serves its last good answer, logs
one WARNING line (no traceback, so self-heal sees a rate rather than a crash) and is read again on the next pass. A
board ``session_tasks`` never read for the query is retried for a bounded budget, then raises: unknown, never "no
work". A corrupt or missing board is not busy and still raises.

State is per process; ``kanban_session_activity`` resets it with its caches.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from hermes_cli.kanban_generation import is_busy
from hermes_cli.kanban_lifecycle_diagnostics import busy_details
from hermes_cli.kanban_session_activity_revalidate import is_busy_key

_log = logging.getLogger("hermes_cli.kanban_session_activity")

# .reused: a busy board's last good counts stood in for a read in this thread's recompute. Such counts must not be
# revalidated past the TTL on an unchanged fingerprint: only a real read of that board may extend them.
reuse_sink = threading.local()

_tasks_memo: dict[tuple, list[dict[str, Any]]] = {}  # last good per-board task list, by (board, ids, statuses)
_lean_tasks_memo: dict[tuple, list[dict[str, Any]]] = {}  # outcome lean reader; never the sidebar namespace
_tasks_lock = threading.Lock()
TASKS_MEMO_MAX = 512
# One request's shared budget for re-reading busy boards that have no last good list for the query (well inside the
# Desktop's 15 s request timeout); the pause only bounds an error that returns without waiting.
FIRST_READ_BUSY_BUDGET_S = 3.0
FIRST_READ_RETRY_PAUSE_S = 0.05


def request_deadline() -> float:
    """The monotonic deadline one ``session_tasks`` request shares across every board it reads."""
    return time.monotonic() + FIRST_READ_BUSY_BUDGET_S


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
        _log.warning("kanban board %s busy; reused last counts kind=%s sqlite_code=%s pid=%s holder=%s",
                     path, *busy_details(exc))
        reuse_sink.reused = True
        return hit[1], False, False
    return reads, complete, True


def _memo(namespace: str) -> dict[tuple, list[dict[str, Any]]]:
    return _lean_tasks_memo if namespace == "lean" else _tasks_memo


def _row_copy(task: dict[str, Any], *, stale: bool = False) -> dict[str, Any]:
    row = dict(task)
    wait = row.get("wait")
    if isinstance(wait, dict):
        row["wait"] = dict(wait)
    if stale:
        row["stale"] = True
    return row


def session_tasks(path: Path, ids: list[str], statuses: tuple[str, ...],
                  read: Callable[[Path, list[str], tuple[str, ...]], list[dict[str, Any]]], *,
                  deadline: float | None = None, memo_namespace: str = "") -> list[dict[str, Any]]:
    """``read(path, ids, statuses)``, or on a busy board its last good list for the same query, every row ``stale``.

    A busy board with no last list for this query (a chat opened for the first time since the backend started) is
    read again until the caller's ``deadline`` (``time.monotonic()``) passes, then raises: one read only waits 500 ms
    for the board, and a lifecycle hold a little longer than that made ``/api/session-kanban-tasks`` 500 and the
    Desktop panel show "Task status unavailable" for every newly opened chat. The deadline belongs to the whole
    request (``request_deadline``), so several busy boards share one budget. ``None`` never retries: callers holding
    a lock (the links projection under ``_links_lock``) must fail fast.

    ``memo_namespace`` default empty keeps the sidebar key ``(path, ids, statuses)``. The outcome lean
    reader uses ``lean`` so the two last-good lists cannot poison each other.
    """
    store = _memo(memo_namespace)
    key = (str(path), tuple(ids), tuple(statuses))
    while True:
        try:
            tasks = read(path, ids, statuses)
            break
        except sqlite3.OperationalError as exc:
            if not is_busy(exc):
                raise
            with _tasks_lock:
                last = store.get(key)
            if last is not None:
                _log.warning("kanban board %s busy; reused last counts kind=%s sqlite_code=%s pid=%s holder=%s",
                             path, *busy_details(exc))
                return [_row_copy(t, stale=True) for t in last]
            now = time.monotonic()
            if deadline is None or now >= deadline:
                raise
            time.sleep(min(FIRST_READ_RETRY_PAUSE_S, deadline - now))  # an instant busy error must not spin
    with _tasks_lock:
        store.pop(key, None)
        store[key] = [_row_copy(t) for t in tasks]
        while len(store) > TASKS_MEMO_MAX:
            store.pop(next(iter(store)))
    return tasks


def reset() -> None:
    with _tasks_lock:
        _tasks_memo.clear()
        _lean_tasks_memo.clear()
