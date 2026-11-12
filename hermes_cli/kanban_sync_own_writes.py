"""Repair-revision steps the sync thread made itself, collected per board file. [daveey]

Hub, 2026-11-12 (t_01a1d02bf3e0d019): after #1461 credited the round's feed applies, the hub's sync thread still
ran the full repair pass on almost every sampled round (~40% of its wall time). Nearly every live revision step
between two rounds was the sync thread's own bookkeeping after it read its inputs: push acknowledgements (outbox
deletes, ``sync_version``), the remote-parent mirror, and pool reservations with the feed pull that follows them
(``kanban_worker_placement.reevaluate``). None of these is the uncaptured local write the scans exist to find.

``credited(conn)`` records, for the calling thread only, every ``kanban_repair_revision`` step made through
``conn`` while the block runs. The recorder is a TEMP trigger: it fires only for statements run on the connection
that created it, and its rows live in that connection's temp schema, so they commit and roll back with the write
that made them (a rolled-back step is never credited). When the block ends outside a transaction its rows move to a
per-file list, so a later round on another connection to the same file still sees them; ``take(path)`` returns
them in order and clears them, and ``kanban_sync_repair_window.decide`` chains them from the credited revision.
Anything the recorder misses (another thread or process, a failed install, a block that ends mid-transaction)
leaves a gap in that chain, which repairs at once.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from typing import Iterator

_TABLE = "kanban_sync_credited_steps"
_TRIGGER = "kanban_sync_credited_step"
_GATE = "hermes_kanban_sync_credit_on"
_active = threading.local()
_lock = threading.Lock()
_STEPS: dict[str, list[tuple[int, int]]] = {}


def _installed(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_temp_master WHERE type = 'trigger' AND name = ?",
                        (_TRIGGER,)).fetchone() is not None


def _install(conn: sqlite3.Connection) -> None:
    key = id(conn)  # the gate answers for this connection and the calling thread only
    conn.create_function(_GATE, 0, lambda: int(key in getattr(_active, "keys", ())))
    conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_TABLE} (before INTEGER NOT NULL, after INTEGER NOT NULL)")
    conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS {_TRIGGER} AFTER UPDATE OF revision ON main.kanban_repair_revision "
                 f"WHEN {_GATE}() BEGIN INSERT INTO {_TABLE} VALUES (OLD.revision, NEW.revision); END")


def _drain(conn: sqlite3.Connection) -> None:
    """Move ``conn``'s committed recorded steps to its file's list (only outside a transaction)."""
    if conn.in_transaction:
        return
    name = conn.execute("PRAGMA database_list").fetchone()[2]
    steps = [(int(b), int(a)) for b, a in conn.execute(f"SELECT before, after FROM temp.{_TABLE} ORDER BY rowid")]
    if not steps:
        return
    conn.execute(f"DELETE FROM temp.{_TABLE}")
    with _lock:
        _STEPS.setdefault(name, []).extend(steps)


@contextlib.contextmanager
def credited(conn: sqlite3.Connection) -> Iterator[None]:
    """Credit this thread's repair-revision steps through ``conn`` until the block ends. A board without the
    revision table yet (or any install error) records nothing, so its steps stay uncredited."""
    try:
        if not _installed(conn):
            _install(conn)
    except sqlite3.Error:
        yield
        return
    keys = _active.__dict__.setdefault("keys", set())
    added = id(conn) not in keys
    keys.add(id(conn))
    try:
        yield
    finally:
        if added:
            keys.discard(id(conn))
            with contextlib.suppress(sqlite3.Error):
                _drain(conn)


def take(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """The credited (before, after) steps recorded on ``conn``'s file since the last call, oldest first (any
    connection to it); forgets them."""
    try:
        name = conn.execute("PRAGMA database_list").fetchone()[2]
    except sqlite3.Error:
        return []
    with _lock:
        return _STEPS.pop(name, [])


def reset() -> None:
    """Tests (``reset_board_watch``): forget every recorded step."""
    with _lock:
        _STEPS.clear()
