"""Thread-local kept read connections for Kanban board read callers. [daveey]

A ``connect_closing`` open of a board costs a realpath, the board lifecycle lock, generation recovery, the
SQLite open, eight PRAGMAs, the OFD lock guard (``kanban_lockguard._apply``, whose fd-table scan grows with every
descriptor the process holds) and ``kanban_generation.prepare_connection``, and the close undoes it all. Loops that
only read paid that on every pass: 11.4% of the hub gateway's samples were connection opens (t_01a1d563324d7e8e
card I). :func:`reading` keeps one connection per (thread, board file) instead, so ``prepare_connection`` and the
guard run once per kept connection, not once per read.

A kept connection is handed out only while it still reads the live board. Before every borrow it is checked
against, and reopened through :func:`kanban_db_connect.connect` on any change of:

- the board file's ``(st_dev, st_ino)`` (a file replaced or deleted under the connection);
- its ``-shm`` identity (``kanban_generation`` recorded it at open; a new or missing ``-shm`` is a new WAL
  generation, kanban-759104eb56);
- the path's place in ``_INITIALIZED_PATHS`` (``init_db``/``repair_db``/a lost schema drop it: the next open
  re-runs the init path) and the generation's poison flag;
- the connection itself (closed by generation recovery: ``kanban_generation.forget`` drops it).

Generation recovery in another thread must be able to retire an idle kept connection, or a stale one parked on an
idle thread would refuse every other thread's open (``DeletedWalGenerationError``). So kept connections are opened
with ``check_same_thread=False`` and registered with ``kanban_generation.register_reader_cache``; a borrowed one is
never closed under its borrower (recovery is told to retry, as for ``kanban_session_activity``'s cache).

Kept connections are ``PRAGMA query_only``: a caller that writes through one fails loudly instead of
committing through a shared handle. A body that leaves a transaction open, or raises ``sqlite3.Error``, loses its
connection. A nested borrow of the same board on one thread, a fenced (delegated) board and a read-only-schema
board get an ordinary per-call connection.

The lock guard's fd scan itself is untouched (design C14): it is a corruption guard, and fewer opens is the only
saving claimed for it here.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import weakref
from collections import OrderedDict
from pathlib import Path
from typing import Iterator, Optional

from hermes_cli import kanban_generation as _gen

# Boards one thread keeps at once; the oldest idle one is closed past this (each holds db + -wal + -shm fds).
MAX_PER_THREAD = 16


class _Entry:
    __slots__ = ("__weakref__", "closed", "conn", "ident", "key", "lock")

    def __init__(self, key: str, conn: sqlite3.Connection, ident: tuple[int, int]) -> None:
        self.key = key
        self.conn = conn
        self.ident = ident
        self.lock = threading.Lock()  # held while borrowed; never re-entered (a nested borrow gets its own conn)
        self.closed = False


_tls = threading.local()
_registry: weakref.WeakSet[_Entry] = weakref.WeakSet()
_registry_lock = threading.Lock()  # map operations only: never held across SQL, a close or another lock
# Entries of exited threads: closed by the next borrow on a live thread, never from a finalizer (a close takes the
# board lifecycle lock, and a finalizer can run on any thread while that thread holds another).
_orphans: list[_Entry] = []

# Observability for tests and P1 readback: opens = kept connections opened, reuses = borrows served warm.
stats = {"opens": 0, "reuses": 0, "reopens": 0, "fallbacks": 0}


def _orphan(entries: OrderedDict[str, _Entry]) -> None:
    with _registry_lock:
        _orphans.extend(entries.values())
    entries.clear()


class _ThreadEntries:
    """Per-thread map; when the thread exits its entries move to ``_orphans``."""

    def __init__(self) -> None:
        self.entries: OrderedDict[str, _Entry] = OrderedDict()
        weakref.finalize(self, _orphan, self.entries)


def _mine() -> OrderedDict[str, _Entry]:
    holder = getattr(_tls, "holder", None)
    if holder is None:
        holder = _tls.holder = _ThreadEntries()
    return holder.entries


def _close(entry: _Entry) -> None:
    """Close *entry*'s connection; the caller holds ``entry.lock`` (or the entry is unreachable)."""
    if entry.closed:
        return
    entry.closed = True
    with _registry_lock:
        _registry.discard(entry)
    with contextlib.suppress(Exception):
        entry.conn.close()


def _drain_orphans() -> None:
    if not _orphans:
        return
    with _registry_lock:
        dead, _orphans[:] = list(_orphans), []
    for entry in dead:
        if entry.lock.acquire(blocking=False):
            try:
                _close(entry)
            finally:
                entry.lock.release()


def _file_identity(path: str) -> Optional[tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _still_live(entry: _Entry) -> bool:
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_lockguard import shm_identity

    if entry.closed or entry.conn not in _gen._connections:
        return False  # closed by generation recovery (or never registered)
    key = entry.key
    if key not in kbc._INITIALIZED_PATHS or key in _gen._poisoned:
        return False
    if _file_identity(key) != entry.ident:
        return False  # replaced, moved or deleted under the connection
    return shm_identity(key) == getattr(entry.conn, "_hermes_generation_shm", None)


def _evict(path: str) -> None:
    """``kanban_generation.recover`` hook: retire every idle kept connection to *path*, from any thread."""
    with _registry_lock:
        entries = [e for e in _registry if e.key == path and not e.closed]
    for entry in entries:
        if not entry.lock.acquire(blocking=False):
            raise _gen.DeletedWalGenerationError(f"kanban DB {path}: a kept reader is still borrowed; retry")
        try:
            _close(entry)
        finally:
            entry.lock.release()


_gen.register_reader_cache(_evict)


def _open(path: Path, key: str, board: Optional[str]) -> tuple[sqlite3.Connection, Optional[_Entry]]:
    """Open through ``connect`` (every board check and the init path stay in one place). Returns the connection
    and its entry, or no entry when this open must not be kept."""
    from agent.delegation_context import kanban_path_is_fenced
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_schema_owner as schema_owner

    if kanban_path_is_fenced(path):
        stats["fallbacks"] += 1
        return kbc.connect(path, board=board), None
    before = _file_identity(key)
    conn = kbc.connect(path, board=board, check_same_thread=False)
    keepable = (before is not None and key in kbc._INITIALIZED_PATHS and key not in schema_owner.READ_ONLY
                and getattr(conn, "_hermes_generation_path", None) == key and _file_identity(key) == before)
    if not keepable or before is None:
        stats["fallbacks"] += 1
        return conn, None
    try:
        conn.execute("PRAGMA query_only=ON")
    except BaseException:
        conn.close()
        raise
    stats["opens"] += 1
    return conn, _Entry(key, conn, before)


@contextlib.contextmanager
def reading(db_path: Optional[Path] = None, *, board: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """Drop-in for ``kanban_db_connect.connect_closing`` in callers that only READ the board.

    Same path resolution, row factory and text factory; the connection is kept for this thread's next read.
    Writes fail (``query_only``)."""
    from hermes_cli import kanban_db as kb

    _drain_orphans()
    path = Path(db_path) if db_path is not None else kb.kanban_db_path(board=board)
    key = _gen._resolved(os.fspath(path))
    mine = _mine()
    entry = mine.get(key)
    if entry is not None and not entry.lock.acquire(blocking=False):
        # A nested borrow of this board on this thread: the outer borrow keeps the entry, the inner one gets an
        # ordinary connection.
        stats["fallbacks"] += 1
        with contextlib.closing(_plain(path, board)) as conn:
            yield conn
        return
    if entry is not None:
        if _still_live(entry):
            stats["reuses"] += 1
        else:
            mine.pop(key, None)
            _close(entry)
            entry.lock.release()
            entry = None
            stats["reopens"] += 1
    if entry is None:
        conn, entry = _open(path, key, board)
        if entry is None:
            with contextlib.closing(conn):
                yield conn
            return
        entry.lock.acquire()
        with _registry_lock:
            _registry.add(entry)
        mine[key] = entry
        while len(mine) > MAX_PER_THREAD:
            old_key, old = next(iter(mine.items()))
            mine.pop(old_key)
            if old.lock.acquire(blocking=False):
                try:
                    _close(old)
                finally:
                    old.lock.release()
    mine.move_to_end(key)
    conn = entry.conn
    keep = False
    try:
        conn.row_factory = sqlite3.Row
        conn.text_factory = kb._lossy_text
        yield conn
        keep = not conn.in_transaction
    except sqlite3.Error:
        raise
    except BaseException:
        keep = not entry.closed and not _in_transaction(conn)
        raise
    finally:
        if not keep:
            if mine.get(key) is entry:
                mine.pop(key, None)
            _close(entry)
        entry.lock.release()


def _in_transaction(conn: sqlite3.Connection) -> bool:
    try:
        return conn.in_transaction
    except sqlite3.Error:
        return True


def _plain(path: Path, board: Optional[str]) -> sqlite3.Connection:
    from hermes_cli import kanban_db_connect as kbc

    return kbc.connect(path, board=board)


def close_all() -> int:
    """Close every idle kept connection on every thread (tests, shutdown). Returns how many closed."""
    _drain_orphans()
    with _registry_lock:
        entries = [e for e in _registry if not e.closed]
    closed = 0
    for entry in entries:
        if entry.lock.acquire(blocking=False):
            try:
                if not entry.closed:
                    _close(entry)
                    closed += 1
            finally:
                entry.lock.release()
    holder = getattr(_tls, "holder", None)
    if holder is not None:
        for key in [k for k, e in holder.entries.items() if e.closed]:
            holder.entries.pop(key, None)
    return closed


__all__ = ["MAX_PER_THREAD", "close_all", "reading", "stats"]
