"""Content-only repair reuse; the outer cursor-dependent repair window stays authoritative."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import sqlite3
import threading

from hermes_cli.kanban_sync_repair_revision import ensure_revision

_LIMIT = 32
_done: OrderedDict[str, tuple] = OrderedDict()
_lock = threading.Lock()


def reset() -> None:
    with _lock:
        _done.clear()


def _revision(conn: sqlite3.Connection) -> tuple | None:
    if conn.in_transaction:
        return None
    name = conn.execute("PRAGMA database_list").fetchone()[2]
    if not name:
        return None
    stat = Path(name).stat()
    row = conn.execute(
        "SELECT revision, schema_version FROM kanban_repair_revision, pragma_schema_version WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    # Main-file timestamps deliberately over-invalidate at checkpoint. Unlike
    # dev/inode/counter alone they also fence an external in-place rebuild.
    # Connection identity also fences a pathname replaced during this borrow:
    # a fresh handle must not inherit a stamp combining new stat with old rows.
    return (conn, name, stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size, tuple(row))


def run(live, replica, gateway, *, path, inputs, rebuilt=False) -> dict[str, list[str]]:
    from hermes_cli import kanban_sync_client as client

    key = str(path)
    stamp = None
    try:
        # Repair counter triggers independently of capture triggers: a capture
        # gap is exactly what these scans must find. Unknown inputs never skip.
        if not replica.in_transaction:
            ensure_revision(replica)
        left, right = _revision(live), _revision(replica)
        if inputs is not None and left is not None and right is not None:
            stamp = left, right, tuple(inputs[2:])
    except (OSError, sqlite3.Error):
        stamp = None
    with _lock:
        previous = _done.pop(key, None)
        if not rebuilt and stamp is not None and previous == stamp:
            _done[key] = stamp
            return {}
    # Save the PRE-scan key only after both calls succeed. Own repair writes and
    # concurrent relevant commits therefore re-arm the next pass, never vanish.
    repaired = client.repair_divergence(live, replica, gateway)
    client.repair_missing_children(live, replica, gateway)
    if stamp is not None:
        with _lock:
            _done[key] = stamp
            while len(_done) > _LIMIT:
                _done.popitem(last=False)
    return repaired
