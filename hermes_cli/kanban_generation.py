"""Retire every known handle to a dead Kanban WAL generation before opening another.

SQLite shares its WAL-index mapping per inode, not per Connection. Opening one replacement
while a sibling still holds the old mapping simply inherits the broken view. Never force-close
a foreign thread's handle or roll back an outstanding transaction to get past that boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import sqlite3
import threading
import time
import weakref
from urllib.parse import unquote

_lock = threading.RLock()
_connections: weakref.WeakSet = weakref.WeakSet()
_reader_caches: list = []
_poisoned: set[str] = set()
_lifecycle_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def lifecycle_lock(database):
    """Serialize one board's open/close transitions, never unrelated databases.

    Callers retain the lock through the whole transition and on returned handles.
    Reentrant because recovery may close a same-thread stale handle. The registry
    lock is released before waiting, reading SQL or invoking cache eviction.
    """
    path = _path(database)
    if path is None:
        return threading.RLock()
    with _lock:
        lock = _lifecycle_locks.get(path)
        if lock is None:
            lock = threading.RLock()
            _lifecycle_locks[path] = lock
        return lock


class BoardLifecycleBusy(sqlite3.OperationalError):
    """Another thread holds this board's open/close transition. [daveey]

    Carries ``SQLITE_BUSY`` like a native busy error, so every busy check (``retry_open``, readers that degrade a
    busy board to its last good answer) treats it as "busy, retry later" rather than a broken board
    (kanban-359833be53: without the code one busy board marked every sidebar row ``kanban_unknown``)."""

    def __init__(self, message="database is locked: board lifecycle is busy"):
        super().__init__(message)
        self.sqlite_errorcode = sqlite3.SQLITE_BUSY
        self.sqlite_errorname = "SQLITE_BUSY"


def is_busy(exc) -> bool:
    """True for a busy/locked board (native SQLite or ``BoardLifecycleBusy``), not a corrupt or missing one."""
    return (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


@contextmanager
def lifecycle_access(database, timeout):
    """A reader keeps its own busy budget even behind a long-timeout opener."""
    lock = lifecycle_lock(database)
    if not lock.acquire(timeout=max(0.0, float(timeout))):
        raise BoardLifecycleBusy()
    try:
        yield lock
    finally:
        lock.release()


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


def remember(conn, *, strict=False) -> None:
    from hermes_cli.kanban_lockguard import _db_file, shm_identity

    path = _db_file(conn)
    if path is None:
        return
    # Opening the WAL-index before recording its identity also covers unguarded/unsupported
    # connections; database_list alone does not necessarily attach the shared-memory file.
    try:
        conn.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.Error:
        if strict:
            raise
        return
    with _lock:
        conn._hermes_generation_path = os.path.realpath(path)
        conn._hermes_generation_shm = shm_identity(path)
        conn._hermes_generation_thread = threading.get_ident()
        _connections.add(conn)


def prepare_connection(conn, timeout):
    """Never sleep inside SQLite while holding a board lifecycle lock.

    A busy attempt closes completely before retrying outside the lock. This
    lets existing readers close without waiting for a native writer's budget.
    """
    from hermes_cli.kanban_lockguard import guard

    conn.execute("PRAGMA busy_timeout=0")
    guard(conn, strict=True)
    remember(conn, strict=True)
    conn.execute(f"PRAGMA busy_timeout={max(0, int(float(timeout) * 1000))}")


def retry_open(open_once, timeout):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            return open_once(max(0.0, deadline - time.monotonic()))
        except sqlite3.OperationalError as exc:
            if not is_busy(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
        # No board, tracking, dispatch or cache registry lock held here.
        time.sleep(min(0.01, remaining))


def forget(conn) -> None:
    with _lock:
        _connections.discard(conn)


def register_reader_cache(evict) -> None:
    """A cache eviction must wait for its borrowers before closing its handles."""
    with _lock:
        if evict not in _reader_caches:
            _reader_caches.append(evict)


@contextmanager
def _cache_registry_available(lock):
    # Recovery runs under a board lifecycle lock. Never wait for a foreign
    # cache owner (including legacy callers) that might be opening that board.
    if not lock.acquire(blocking=False):
        raise DeletedWalGenerationError("kanban reader registry is busy; retry generation recovery")
    try:
        yield
    finally:
        lock.release()


def evict_readers(path: str) -> None:
    from hermes_cli import kanban_session_activity as activity

    with _cache_registry_available(activity._RO_CACHE_LOCK):
        entries = [(key, entry) for key, entry in activity._RO_CACHE.items() if os.path.realpath(key) == path]
    for key, entry in entries:
        # Never wait while a caller might hold the cache lock: borrowers take that lock
        # on error. A busy borrower owns its lifecycle and will release it for a retry.
        if not entry[2].acquire(blocking=False):
            raise DeletedWalGenerationError(f"kanban DB {path}: stale reader is still borrowed; retry")
        try:
            with _cache_registry_available(activity._RO_CACHE_LOCK):
                if activity._RO_CACHE.get(key) is not entry:
                    continue
                if entry[0].in_transaction:
                    raise DeletedWalGenerationError(f"kanban DB {path}: stale cached reader has a transaction; retry")
                del activity._RO_CACHE[key]
            entry[0].close()
        finally:
            entry[2].release()


def open_ro(path, check_same_thread: bool = True, *, timeout=0.5) -> sqlite3.Connection:
    """Never create a missing board; retry read/write only to initialize missing WAL sidecars."""
    from hermes_cli import kanban_lockguard

    for mode in ("ro", "rw"):
        conn = None
        try:
            conn = kanban_lockguard.connect(path.resolve().as_uri() + f"?mode={mode}",
                                           uri=True, timeout=timeout, check_same_thread=check_same_thread)
            conn.execute("PRAGMA query_only = ON")
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            return conn
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            if getattr(exc, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise  # mode=rw cannot resolve a busy writer; don't double the budget.
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
    # Snapshot the handles BEFORE reading the live identity. The other order races a concurrent first open:
    # ``current`` is read while no -shm exists (None), the opener then creates it and registers its handle
    # with the new identity, and this call would call that healthy handle stale and evict it.
    handles = _handles(path)
    current = shm_identity(path)
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
