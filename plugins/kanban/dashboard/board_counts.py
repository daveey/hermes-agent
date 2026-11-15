"""Switcher counts need no schema reconciliation or writer initialization.

GET /boards is polled continuously, and every board's count query used to open a
fresh guarded connection. The last counts per board file are kept, keyed on
``board_rows.content_version`` (observer generation plus the P2 change cursor, or the
commit key on a legacy board without one). An unchanged board costs the key read on a
retained connection and opens nothing. There is no timer: the key alone decides.

A changed board is updated per task, not re-counted. The entry keeps each task's status
and the P2 cursor it was read at. The next read takes ``changed_since`` and the status
of only those tasks in ONE snapshot, then moves each one between buckets. The whole
table is read only when the cursor declines (FULL: first read, new file or WAL
generation, schema change, an older writer's trigger) or more than ``DELTA_MAX`` tasks
moved. A legacy board (no cursor) keeps the exact ``GROUP BY status`` read.
[daveey] card E stage7 (t_01a1d59e5543daa1): the whole-table count of a changed board
grew 1.51x from 1k to 20k tasks on a 440 MB WAL.
"""
from collections import OrderedDict
from contextlib import closing
import sqlite3
import threading
from typing import Optional

from hermes_cli import kanban_db
from hermes_cli import kanban_task_change
from hermes_cli.kanban_generation import open_ro
from plugins.kanban.dashboard import board_rows

MAX_CACHED = 64  # board files with remembered counts
DELTA_MAX = 512  # more changed tasks than this re-reads the board in full
_CHUNK = 500  # ids per IN list (below SQLite's default 999 host parameters)

_lock = threading.Lock()
# path -> (key, counts, state); state is None (legacy board) or (cursor, {task id: status})
_cached: "OrderedDict[str, tuple[tuple, dict[str, int], Optional[tuple]]]" = OrderedDict()
_path_locks: dict[str, threading.Lock] = {}


def _tally(statuses: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for status in statuses.values():
        out[status] = out.get(status, 0) + 1
    return out


def _query(path, prev: Optional[tuple] = None) -> tuple[dict[str, int], Optional[tuple]]:
    """``(counts, state)`` of ``path``. ``prev`` is the last ``(counts, state)`` or None.

    Cursor, changed ids and their statuses come from one read snapshot. The caller holds
    the board's path lock and has unpublished ``prev``: its statuses map is updated in
    place, and only after every read succeeded.
    """
    with closing(open_ro(path)) as conn:
        if conn.in_transaction:  # never hold an older snapshot than this read's own
            conn.rollback()
        conn.execute("BEGIN")
        try:
            prev_state = prev[1] if prev is not None else None
            changed, cursor = kanban_task_change.changed_since(
                conn, prev_state[0] if prev_state is not None else None)
            if cursor is None:  # legacy board or an unobservable file identity: the exact count
                return {status: int(n) for status, n in conn.execute(
                    "SELECT status, COUNT(*) FROM tasks GROUP BY status")}, None
            if prev is None or prev_state is None or not isinstance(changed, frozenset) \
                    or len(changed) > DELTA_MAX:  # FULL, or no retained statuses to move
                statuses = dict(conn.execute("SELECT id, status FROM tasks"))
                return _tally(statuses), (cursor, statuses)
            ids = list(changed)
            now: dict = {}
            for i in range(0, len(ids), _CHUNK):
                part = ids[i:i + _CHUNK]
                now.update(conn.execute(
                    f"SELECT id, status FROM tasks WHERE id IN ({','.join('?' * len(part))})", part))
        finally:
            if conn.in_transaction:
                conn.rollback()
    counts, statuses = dict(prev[0]), prev_state[1]
    for tid in ids:
        if tid in statuses:
            before = statuses.pop(tid)
            left = counts.get(before, 0) - 1
            if left > 0:
                counts[before] = left
            else:
                counts.pop(before, None)
        if tid in now:  # absent: deleted
            after = statuses[tid] = now[tid]
            counts[after] = counts.get(after, 0) + 1
    return counts, (cursor, statuses)


def _path_lock(key: str) -> threading.Lock:
    with _lock:
        lock = _path_locks.get(key)
        if lock is None:
            lock = _path_locks[key] = threading.Lock()
        return lock


def board_counts(slug: str) -> dict[str, int]:
    """Read existing tasks.status, including legacy boards; never create a store.

    Use the generation-guarded query-only connection, including its missing WAL
    sidecar fallback. Writer connect still owns integrity checks and migrations.
    The key is read before the query, so a commit that lands during the query
    is read again next time. An unobservable board (key None) and a failed
    query store nothing. A hit returns a copy: callers mutate and serialize it.
    One refresh per board file at a time, under its path lock: the entry is taken out
    while it is updated, so a failure leaves nothing (the next read is FULL).
    """
    try:
        path = kanban_db.kanban_db_path(board=slug)
        key = str(path)
        ver = board_rows.content_version(key)
        if ver is None:
            with _lock:
                _cached.pop(key, None)
            return _query(path)[0]
        with _path_lock(key):
            with _lock:
                hit = _cached.get(key)
                if hit is not None and hit[0] == ver:
                    _cached.move_to_end(key)
                    return dict(hit[1])
                if hit is not None:
                    del _cached[key]
            counts, state = _query(path, (hit[1], hit[2]) if hit is not None else None)
            with _lock:
                _cached[key] = (ver, counts, state)
                _cached.move_to_end(key)
                while len(_cached) > MAX_CACHED:
                    _cached.popitem(last=False)
            return dict(counts)
    except (OSError, ValueError, sqlite3.Error):
        return {}


def clear() -> None:
    """Forget every remembered count (tests)."""
    with _lock:
        _cached.clear()
