"""Switcher counts need no schema reconciliation or writer initialization.

GET /boards is polled continuously, and every board's count query used to open a
fresh guarded connection. The last counts per board file are kept, keyed on
``board_rows.version`` (observer generation, file identity, schema_version and
``PRAGMA data_version``). Any commit by another connection moves that version, so
an unchanged board costs two PRAGMAs on a retained connection and a changed board
is read again on the next poll. There is no timer: the key alone decides.
"""
from collections import OrderedDict
from contextlib import closing
import sqlite3
import threading

from hermes_cli import kanban_db
from hermes_cli.kanban_generation import open_ro
from plugins.kanban.dashboard import board_rows

MAX_CACHED = 64  # board files with remembered counts (one small dict each)

_lock = threading.Lock()
_cached: "OrderedDict[str, tuple[tuple, dict[str, int]]]" = OrderedDict()


def _query(path) -> dict[str, int]:
    with closing(open_ro(path)) as conn:
        return {status: int(count) for status, count in conn.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status")}


def board_counts(slug: str) -> dict[str, int]:
    """Read existing tasks.status, including legacy boards; never create a store.

    Use the generation-guarded query-only connection, including its missing WAL
    sidecar fallback. Writer connect still owns integrity checks and migrations.
    The version is read before the query, so a commit that lands during the query
    is read again next time. An unobservable board (version None) and a failed
    query store nothing. A hit returns a copy: callers mutate and serialize it.
    """
    try:
        path = kanban_db.kanban_db_path(board=slug)
        key = str(path)
        ver = board_rows.content_version(key)
        if ver is not None:
            with _lock:
                hit = _cached.get(key)
                if hit is not None and hit[0] == ver:
                    _cached.move_to_end(key)
                    return dict(hit[1])
        counts = _query(path)
    except (OSError, ValueError, sqlite3.Error):
        return {}
    if ver is not None:
        with _lock:
            _cached[key] = (ver, dict(counts))
            _cached.move_to_end(key)
            while len(_cached) > MAX_CACHED:
                _cached.popitem(last=False)
    return counts


def clear() -> None:
    """Forget every remembered count (tests)."""
    with _lock:
        _cached.clear()
