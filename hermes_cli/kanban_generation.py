"""Retire every known handle to a dead Kanban WAL generation before opening another.

SQLite shares its WAL-index mapping per inode, not per Connection. Opening one replacement
while a sibling still holds the old mapping simply inherits the broken view. Never force-close
a foreign thread's handle or roll back an outstanding transaction to get past that boundary.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import weakref
from urllib.parse import unquote

_lock = threading.RLock()
_connections: weakref.WeakSet = weakref.WeakSet()
_reader_caches: list = []
_poisoned: set[str] = set()


class DeletedWalGenerationError(sqlite3.OperationalError):
    """Retry after the remaining active handles leave the dead WAL generation."""


def _path(database) -> str | None:
    value = os.fspath(database)
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if value.startswith("file:"):
        value = unquote(value[5:].split("?", 1)[0])
    if not value or value == ":memory:":
        return None
    return os.path.realpath(value)


def remember(conn) -> None:
    from hermes_cli.kanban_lockguard import _db_file, shm_identity

    path = _db_file(conn)
    if path is None:
        return
    # Opening the WAL-index before recording its identity also covers unguarded/unsupported
    # connections; database_list alone does not necessarily attach the shared-memory file.
    try:
        conn.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.Error:
        return
    with _lock:
        conn._hermes_generation_path = os.path.realpath(path)
        conn._hermes_generation_shm = shm_identity(path)
        conn._hermes_generation_thread = threading.get_ident()
        _connections.add(conn)


def forget(conn) -> None:
    with _lock:
        _connections.discard(conn)


def register_reader_cache(evict) -> None:
    """A cache eviction must wait for its borrowers before closing its handles."""
    with _lock:
        if evict not in _reader_caches:
            _reader_caches.append(evict)


def evict_readers(path: str) -> None:
    from hermes_cli import kanban_session_activity as activity

    with activity._RO_CACHE_LOCK:
        entries = [(key, entry) for key, entry in activity._RO_CACHE.items() if os.path.realpath(key) == path]
    for key, entry in entries:
        # Never wait while a caller might hold the cache lock: borrowers take that lock
        # on error. A busy borrower owns its lifecycle and will release it for a retry.
        if not entry[2].acquire(blocking=False):
            raise DeletedWalGenerationError(f"kanban DB {path}: stale reader is still borrowed; retry")
        try:
            with activity._RO_CACHE_LOCK:
                if activity._RO_CACHE.get(key) is not entry:
                    continue
                if entry[0].in_transaction:
                    raise DeletedWalGenerationError(f"kanban DB {path}: stale cached reader has a transaction; retry")
                del activity._RO_CACHE[key]
            entry[0].close()
        finally:
            entry[2].release()


def open_ro(path, check_same_thread: bool = True) -> sqlite3.Connection:
    """Never create a missing board; retry read/write only to initialize missing WAL sidecars."""
    from hermes_cli import kanban_lockguard

    for mode in ("ro", "rw"):
        conn = None
        try:
            conn = kanban_lockguard.connect(path.resolve().as_uri() + f"?mode={mode}",
                                           uri=True, timeout=0.5, check_same_thread=check_same_thread)
            conn.execute("PRAGMA query_only = ON")
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            return conn
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            if isinstance(exc, DeletedWalGenerationError) or not isinstance(exc, sqlite3.OperationalError) or mode == "rw":
                raise
    raise AssertionError("unreachable")


def _handles(path):
    with _lock:
        return [c for c in _connections if c._hermes_generation_path == path]


def recover(database) -> None:
    from hermes_cli.kanban_lockguard import shm_identity, stranded_sidecars

    path = _path(database)
    if path is None:
        return
    current = shm_identity(path)
    handles = _handles(path)
    stale = any(c._hermes_generation_shm is not None and c._hermes_generation_shm != current for c in handles)
    with _lock:
        poisoned = path in _poisoned
    # Also refuse unregistered legacy handles on first/uncached opens. Once poisoned,
    # keep refusing retries and mode=rw fallbacks until the last dead mapping is gone.
    stranded = stranded_sidecars(path) if stale or poisoned or not handles else []
    if not stale and not stranded:
        with _lock:
            _poisoned.discard(path)
        return
    with _lock:
        _poisoned.add(path)
        caches = list(_reader_caches)
    # No registry lock across a cache's borrower lock (close() unregisters under _lock).
    for evict in caches:
        evict(path)
    for conn in _handles(path):
        if conn._hermes_generation_shm is None or conn._hermes_generation_shm == current:
            continue
        if conn._hermes_generation_thread != threading.get_ident() or conn.in_transaction:
            raise DeletedWalGenerationError(
                f"kanban DB {path}: deleted WAL/SHM generation still has an active transaction "
                "or a foreign-thread handle; retry after its owner closes it (not corruption)"
            )
        conn.close()
    if stranded_sidecars(path):
        raise DeletedWalGenerationError(
            f"kanban DB {path}: an unregistered handle still holds a deleted WAL/SHM generation; "
            "refusing to inherit its stale mapping (not corruption)"
        )
    with _lock:
        _poisoned.discard(path)
