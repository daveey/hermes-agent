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
import types
import weakref
from urllib.parse import unquote

from hermes_cli.kanban_lifecycle_diagnostics import LifecycleLock

_lock = threading.RLock()
_connections: weakref.WeakSet = weakref.WeakSet()


class _ReaderCaches:
    """Non-owning callbacks; callers hold _lock while reading or changing entries."""

    def __init__(self):
        self._entries = []

    def _snapshot(self):
        live, entries = [], []
        for entry in self._entries:
            callback = entry() if isinstance(entry, weakref.ReferenceType) else entry
            if callback is not None:
                live.append(callback)
                entries.append(entry)
        self._entries = entries
        return live

    def __iter__(self):
        with _lock:
            return iter(self._snapshot())

    def __len__(self):
        with _lock:
            return len(self._snapshot())

    def discard(self, callback):
        for index, current in enumerate(self._snapshot()):
            if current == callback:
                del self._entries[index]
                return True
        return False

    def append(self, callback):
        self._snapshot()
        try:
            entry = weakref.WeakMethod(callback)
        except TypeError:
            owner = getattr(callback, "__self__", None)
            if owner is not None and not isinstance(owner, types.ModuleType):
                # A bound method whose owner has no weakref slot (``__slots__``, ``list.append``): a weak
                # reference to the method object itself would die with the caller's expression.
                entry = callback
            else:
                try:
                    entry = weakref.ref(callback)
                except TypeError:
                    entry = callback  # Some extension callables cannot be weakly referenced.
        self._entries.append(entry)


_reader_caches = _ReaderCaches()
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
        return LifecycleLock()
    with _lock:
        lock = _lifecycle_locks.get(path)
        if lock is None:
            lock = LifecycleLock()
            _lifecycle_locks[path] = lock
        return lock


class BoardLifecycleBusy(sqlite3.OperationalError):
    """Another thread holds this board's open/close transition. [daveey]

    Carries ``SQLITE_BUSY`` like a native busy error, so every busy check (``retry_open``, readers that degrade a
    busy board to its last good answer) treats it as "busy, retry later" rather than a broken board
    (kanban-359833be53: without the code one busy board marked every sidebar row ``kanban_unknown``)."""

    def __init__(self, message="database is locked: board lifecycle is busy", *, diagnostic=None):
        super().__init__(message)
        self.sqlite_errorcode = sqlite3.SQLITE_BUSY
        self.sqlite_errorname = "SQLITE_BUSY"
        self.diagnostic = diagnostic or {}


def is_busy(exc) -> bool:
    """True for a busy/locked board (native SQLite or ``BoardLifecycleBusy``), not a corrupt or missing one."""
    return (getattr(exc, "sqlite_errorcode", 0) or 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


@contextmanager
def lifecycle_access(database, timeout):
    """A reader keeps its own busy budget even behind a long-timeout opener."""
    lock = lifecycle_lock(database)
    started = time.monotonic()
    if not lock.acquire(timeout=max(0.0, float(timeout))):
        raise BoardLifecycleBusy(diagnostic={
            "pid": os.getpid(), "sampled_at": time.time(),
            "wait_ms": round((time.monotonic() - started) * 1000, 3),
            **lock.snapshot(),
        })
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
    return _resolved(value)


# Board paths are resolved on every cached read, under the board lock in ``recover``. ``os.path.realpath``
# makes one GIL-releasing syscall per path component; with a hundred busy serve threads each one waits out
# the switch interval, which was a top board-lock holder on the hub after the fd-scan fix (t_01a1ce5c1d7bfe42).
# An absolute path's resolution is reused for ``_RESOLVE_TTL_S``. A miss uses a GIL-held libc realpath so the
# lock is not held across that walk; a symlink retargeted under a live board is seen within the TTL (board
# paths are not retargeted in practice). Relative paths depend on the cwd: never cached.
# Plain dict get/set/clear are atomic under the GIL; a racing clear only costs one extra resolve.
_RESOLVE_TTL_S = 30.0
_CACHE_MAX = 256
_clock = time.monotonic
_resolved_paths: dict[str, tuple[str, float]] = {}


def _resolve_now(value: str) -> str:
    """Resolve ``value`` without releasing the GIL when the native sweep is on.

    The cache in ``_resolved`` skips this on a hit. A miss still runs under the board lifecycle
    lock, so it must not call ``os.path.realpath`` (one releasing syscall per component).
    """
    from hermes_state_lockguard_fdscan import held_realpath

    held = held_realpath(value)
    if held is not None:
        return held
    return os.path.realpath(value)


def _resolved(value: str) -> str:
    if not os.path.isabs(value):
        return _resolve_now(value)
    now = _clock()
    hit = _resolved_paths.get(value)
    if hit is not None and now - hit[1] < _RESOLVE_TTL_S:
        return hit[0]
    real = _resolve_now(value)
    if len(_resolved_paths) >= _CACHE_MAX:
        _resolved_paths.clear()
    _resolved_paths[value] = (real, now)
    return real


# A clean ``stranded_sidecars`` scan used to read every ``/proc/self/fd`` link with ``os.readlink`` under the
# board lock (~7 s for 600 fds behind 4 busy threads). The miss path is now a GIL-held sweep. A handle is
# stranded only when the board's ``-shm`` or ``-wal`` is replaced or deleted, which changes that file's
# ``(st_dev, st_ino)`` (a held inode cannot be reused); so a clean verdict is still reused while both
# identities are unchanged, for at most ``_CLEAN_SCAN_TTL_S``. A positive verdict is never reused.
_CLEAN_SCAN_TTL_S = 5.0
_clean_scans: dict[str, tuple[object, float]] = {}


def _file_identity(path: str):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _stranded(path, current, scan):
    key = (current, _file_identity(path + "-wal"))
    hit = _clean_scans.get(path)
    if hit is not None and hit[0] == key and _clock() - hit[1] < _CLEAN_SCAN_TTL_S:
        return []
    found = scan(path)
    if found:
        _clean_scans.pop(path, None)
    else:
        if len(_clean_scans) >= _CACHE_MAX:
            _clean_scans.clear()
        _clean_scans[path] = (key, _clock())
    return found


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
        conn._hermes_generation_path = _resolved(path)
        conn._hermes_generation_shm = shm_identity(path)
        conn._hermes_generation_thread = threading.get_ident()
        _connections.add(conn)


def prepare_connection(conn, timeout):
    """Never sleep inside SQLite while holding a board lifecycle lock.

    A busy attempt closes completely before retrying outside the lock. This
    lets existing readers close without waiting for a native writer's budget.
    """
    from hermes_cli.kanban_lockguard import guard
    from hermes_cli import kanban_sync_cpu as sync_cpu

    conn.execute("PRAGMA busy_timeout=0")
    sync_cpu.mark("prepare.guard", guard, conn, strict=True)
    sync_cpu.mark("prepare.remember", remember, conn, strict=True)
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
    """A cache eviction must wait for its borrowers before closing its handles.

    Registration does not keep ``evict`` alive: a bound method lives as long as its instance, a function as long
    as its module or other owner. A lambda, closure or ``functools.partial`` with no other owner is dropped at
    once, so the caller holds it and passes the same object to ``unregister_reader_cache`` on unload. Bound
    methods whose owner cannot be weakly referenced (``__slots__`` instances, builtin methods like
    ``list.append``) and callables without weak-reference support are held strongly until unregistered.
    """
    with _lock:
        if evict not in _reader_caches:
            _reader_caches.append(evict)


def unregister_reader_cache(evict) -> bool:
    """Remove one registered eviction callback; repeated removal is harmless."""
    with _lock:
        return _reader_caches.discard(evict)


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
        entries = [(key, entry) for key, entry in activity._RO_CACHE.items() if _resolved(key) == path]
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
    if stale or poisoned:
        stranded = stranded_sidecars(path)
    elif not handles:
        stranded = _stranded(path, current, stranded_sidecars)
    else:
        stranded = []
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
