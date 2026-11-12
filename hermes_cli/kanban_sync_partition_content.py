"""Reuse successful partition scans only for unchanged, cursor-eligible content."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading

from hermes_cli.kanban_sync_repair_revision import ensure_revision

_LIMIT = 32
_done: OrderedDict[str, tuple] = OrderedDict()
_lock = threading.Lock()


@dataclass(frozen=True)
class Decision:
    key: str
    stamp: tuple | None
    skip: bool


def reset() -> None:
    with _lock:
        _done.clear()


def _snapshot(conn: sqlite3.Connection) -> tuple | None:
    if conn.in_transaction:
        return None
    name = conn.execute("PRAGMA database_list").fetchone()[2]
    if not name:
        return None
    stat = Path(name).stat()
    # Cursor eligibility and content MUST describe the same SQLite snapshot.
    rows = conn.execute(
        "SELECT revision, schema_version, cursor, mode FROM kanban_repair_revision, "
        "pragma_schema_version, kanban_sync_state WHERE id = 1"
    ).fetchall()
    if len(rows) != 1:
        return None
    revision, schema, cursor, mode = rows[0]
    if any(type(value) is not int or value < 0 for value in (revision, schema, cursor)):
        return None
    if mode not in ("primary", "shadow"):
        return None
    # Match content repair's handle + main-file fences, including checkpoints
    # and same-inode rewrites. Cursor movement itself is NOT content.
    stamp = (conn, name, stat.st_dev, stat.st_ino, stat.st_ctime_ns,
             stat.st_mtime_ns, stat.st_size, (revision, schema), mode)
    return stamp, cursor


def _stamp(live, replica, inputs) -> tuple | None:
    if inputs is None or len(inputs) < 5 or inputs[2] not in ("primary", "shadow"):
        return None
    left, right = _snapshot(live), _snapshot(replica)
    if left is None or right is None or right[1] < left[1] or left[0][-1] != inputs[2]:
        return None
    return (left[0], right[0], tuple(inputs[2:])), (left[1], right[1])


def prepare(live, replica, *, path, inputs, rebuilt=False) -> Decision:
    stamp = None
    try:
        if not replica.in_transaction:
            ensure_revision(replica)
        stamp = _stamp(live, replica, inputs)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        pass  # Unknown metadata must run the original scans, not hide their errors.
    key = str(path)
    with _lock:
        previous = _done.pop(key, None)
        skip = not rebuilt and stamp is not None and previous == stamp[0]
        if skip and stamp is not None:
            _done[key] = stamp[0]
    return Decision(key, stamp, skip)


def commit(decision: Decision, live, replica, *, inputs) -> None:
    if decision.skip or decision.stamp is None:
        return
    try:
        current = _stamp(live, replica, inputs)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return
    # A live cursor can advance during the scans and make chat repair a no-op.
    # Never publish that ineligible pass. Keep the PRE-scan key: own writes and
    # concurrent content commits must re-arm, even when both cursors caught up.
    # Even eligible -> ineligible -> eligible can hide a skipped chat scan.
    # Cursor movement DURING a pass refuses publication, but movement between
    # successful passes is excluded from the semantic memo key.
    if current is None or current[1] != decision.stamp[1]:
        return
    with _lock:
        _done[decision.key] = decision.stamp[0]
        _done.move_to_end(decision.key)
        while len(_done) > _LIMIT:
            _done.popitem(last=False)
