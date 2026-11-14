"""Process-wide pool of kept read connections for Kanban board read callers. [daveey]

A ``connect_closing`` open of a board costs a realpath, the board lifecycle lock, generation recovery, the
SQLite open, eight PRAGMAs, the OFD lock guard (``kanban_lockguard._apply``, whose fd-table scan grows with every
descriptor the process holds) and ``kanban_generation.prepare_connection``, and the close undoes it all. Loops that
only read paid that on every pass: 11.4% of the hub gateway's samples were connection opens (t_01a1d563324d7e8e
card I). :func:`reading` keeps connections instead, so ``prepare_connection`` and the guard run once per kept
connection, not once per read.

Kept connections belong to the process, not to a thread: at most ``MAX_PER_BOARD`` per board file and
``MAX_KEPT`` in total (past it the least recently borrowed idle one is closed). Kept per (thread, board), the
gateway's never-exiting executor threads each ended up holding one connection to every board (daveey-mini
2026-11-14: 12 threads x 4 boards = 48 connections, ~0.9 MB each, t_01a1db28414ae096). A borrow takes an idle
entry of its board without waiting; when every entry of the board is borrowed it gets an ordinary per-call
connection, closed on exit.

A kept connection is handed out only while it still reads the live board. Before every borrow it is checked
against, and reopened through :func:`kanban_db_connect.connect` on any change of:

- the board file's ``(st_dev, st_ino)`` (a file replaced or deleted under the connection);
- its ``-shm`` identity (``kanban_generation`` recorded it at open; a new or missing ``-shm`` is a new WAL
  generation, kanban-759104eb56);
- the path's place in ``_INITIALIZED_PATHS`` (``init_db``/``repair_db``/a lost schema drop it: the next open
  re-runs the init path) and the generation's poison flag;
- the connection itself (closed by generation recovery: ``kanban_generation.forget`` drops it).

Generation recovery must be able to retire an idle kept connection, or a stale one would refuse every other
thread's open (``DeletedWalGenerationError``). So kept connections are opened with ``check_same_thread=False`` and
registered with ``kanban_generation.register_reader_cache``; a borrowed one is never closed under its borrower
(recovery is told to retry, as for ``kanban_session_activity``'s cache).

Kept connections are ``PRAGMA query_only``: a caller that writes through one fails loudly instead of
committing through a shared handle. A body that leaves a transaction open, or raises ``sqlite3.Error``, loses its
connection. A fenced (delegated) board and a read-only-schema board get an ordinary per-call connection.

The lock guard's fd scan itself is untouched (design C14): it is a corruption guard, and fewer opens is the only
saving claimed for it here.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Iterator, Optional

from hermes_cli import kanban_generation as _gen

# Kept connections per board file. Further concurrent borrowers of one board get a per-call connection.
MAX_PER_BOARD = 2
# Kept connections in total (each holds db + -wal + -shm fds and ~1 MB of schema and cache); past this the least
# recently borrowed idle one is closed. A borrowed entry is never closed under its borrower: when every other entry
# is borrowed, the new one is not kept and its own borrow's exit closes it.
MAX_KEPT = 32


class _Entry:
    __slots__ = ("closed", "conn", "ident", "key", "lock")

    def __init__(self, key: str, conn: sqlite3.Connection, ident: tuple[int, int]) -> None:
        self.key = key
        self.conn = conn
        self.ident = ident
        self.lock = threading.Lock()  # held while borrowed; never waited on (a busy entry is skipped)
        self.closed = False


# Every kept entry, least recently borrowed first.
_registry: OrderedDict[_Entry, None] = OrderedDict()
_registry_lock = threading.Lock()  # map operations only: never held across SQL, a close or another lock
# Kept opens in flight per board, so concurrent borrowers never keep more than MAX_PER_BOARD.
_opening: dict[str, int] = {}

# Observability for tests and P1 readback: opens = kept connections opened, reuses = borrows served warm.
stats = {"opens": 0, "reuses": 0, "reopens": 0, "fallbacks": 0}


def _close(entry: _Entry) -> None:
    """Close *entry*'s connection; the caller holds ``entry.lock`` (or the entry is unreachable)."""
    if entry.closed:
        return
    entry.closed = True
    with _registry_lock:
        _registry.pop(entry, None)
    with contextlib.suppress(Exception):
        entry.conn.close()


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
    """``kanban_generation.recover`` hook: retire every idle kept connection to *path*."""
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


def _borrow_idle(key: str) -> Optional[_Entry]:
    """Take an idle kept entry of *key* (its lock acquired), least recently borrowed first; never waits."""
    with _registry_lock:
        candidates = [e for e in _registry if e.key == key and not e.closed]
    for entry in candidates:
        if entry.lock.acquire(blocking=False):
            return entry  # one closed since the snapshot is refused by the caller's ``_still_live``
    return None


def _reserve(key: str) -> bool:
    """Claim one of *key*'s ``MAX_PER_BOARD`` slots for a kept open; False when every slot is taken."""
    with _registry_lock:
        held = sum(1 for e in _registry if e.key == key and not e.closed) + _opening.get(key, 0)
        if held >= MAX_PER_BOARD:
            return False
        _opening[key] = _opening.get(key, 0) + 1
        return True


def _unreserve(key: str, entry: Optional[_Entry] = None) -> None:
    """Release *key*'s open slot; with *entry*, register it in the same step (the slot becomes the entry)."""
    with _registry_lock:
        left = _opening.get(key, 0) - 1
        if left > 0:
            _opening[key] = left
        else:
            _opening.pop(key, None)
        if entry is not None:
            _registry[entry] = None


@contextlib.contextmanager
def reading(db_path: Optional[Path] = None, *, board: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """Drop-in for ``kanban_db_connect.connect_closing`` in callers that only READ the board.

    Same path resolution, row factory and text factory; the connection is kept in the process pool for the next
    read of this board, from any thread. Writes fail (``query_only``)."""
    from hermes_cli import kanban_db as kb

    path = Path(db_path) if db_path is not None else kb.kanban_db_path(board=board)
    key = _gen._resolved(os.fspath(path))
    entry = _borrow_idle(key)
    if entry is not None:
        if _still_live(entry):
            stats["reuses"] += 1
        else:
            _close(entry)
            entry.lock.release()
            entry = None
            stats["reopens"] += 1
    if entry is None:
        if not _reserve(key):
            # Every kept entry of this board is borrowed (a nested borrow included): never wait for one.
            stats["fallbacks"] += 1
            with contextlib.closing(_plain(path, board)) as conn:
                yield conn
            return
        try:
            conn, entry = _open(path, key, board)
        except BaseException:
            _unreserve(key)
            raise
        if entry is None:
            _unreserve(key)
            with contextlib.closing(conn):
                yield conn
            return
        entry.lock.acquire()
        _unreserve(key, entry)
        _enforce_cap(entry)
    with _registry_lock:
        if entry in _registry:
            _registry.move_to_end(entry)
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
        with _registry_lock:
            if entry not in _registry:
                keep = False  # detached while borrowed (the cap): nothing else would close it
        if not keep:
            _close(entry)
        entry.lock.release()


def _enforce_cap(new: _Entry) -> None:
    """Close the least recently borrowed idle entries past ``MAX_KEPT``. A borrowed entry is skipped; when every
    other entry is borrowed, *new* (borrowed by the caller) is detached and its borrow's exit closes it."""
    while True:
        with _registry_lock:
            if len(_registry) <= MAX_KEPT:
                return
            candidates = [e for e in _registry if e is not new]
        for old in candidates:
            if old.lock.acquire(blocking=False):
                try:
                    _close(old)
                finally:
                    old.lock.release()
                break
        else:
            with _registry_lock:
                _registry.pop(new, None)
            return


def _in_transaction(conn: sqlite3.Connection) -> bool:
    try:
        return conn.in_transaction
    except sqlite3.Error:
        return True


def _plain(path: Path, board: Optional[str]) -> sqlite3.Connection:
    from hermes_cli import kanban_db_connect as kbc

    return kbc.connect(path, board=board)


def _close_idle(entries: list[_Entry]) -> int:
    """Close each of *entries* that is idle and still open; returns how many closed."""
    closed = 0
    for entry in entries:
        if entry.lock.acquire(blocking=False):
            try:
                if not entry.closed:
                    _close(entry)
                    closed += 1
            finally:
                entry.lock.release()
    return closed


def close_all() -> int:
    """Close every idle kept connection (tests, shutdown). Returns how many closed."""
    with _registry_lock:
        entries = [e for e in _registry if not e.closed]
    return _close_idle(entries)


def release_gone() -> int:
    """Close every idle kept connection whose board file no longer exists; returns how many.

    A borrow revalidates only the board it is asked for, so a board whose directory was removed is never borrowed
    again and its kept connection pinned the deleted db, -wal and -shm until the process exited (hermes-hub
    2026-11-13). A borrowed entry is left to its borrower."""
    from hermes_cli.kanban_sync_board_watch import path_gone

    with _registry_lock:
        entries = [e for e in _registry if not e.closed]
    return _close_idle([e for e in entries if path_gone(e.key)])


__all__ = ["MAX_KEPT", "MAX_PER_BOARD", "close_all", "reading", "release_gone", "stats"]
