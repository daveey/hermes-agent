"""Prepare cached board readers without holding a global lock across SQLite I/O."""
import contextlib
import os
import sqlite3
import threading

from hermes_cli.kanban_generation import DeletedWalGenerationError, lifecycle_access, retry_open


def try_borrow(lock, budget) -> bool:
    """Acquire ``lock`` within ``budget`` seconds (never negative); False when another reader still holds it."""
    return lock.acquire(timeout=max(0.0, budget))


@contextlib.contextmanager
def borrow_lock(lock, budget):
    """Python coordination is part of the reader budget, not an unbounded prelude."""
    if not try_borrow(lock, budget):
        raise sqlite3.OperationalError("database is locked: cached reader is borrowed")
    try:
        yield
    finally:
        lock.release()


def cached_entry(path, busy_budget=0.5, *, reader_cache=None):
    options = {} if reader_cache is None else {'reader_cache': reader_cache}
    return retry_open(lambda remaining: _cached_entry_once(path, remaining, **options), busy_budget)


def borrow_warm(path, *, reader_cache=None):
    """Return an already-borrowed, attested live entry, or None for ordinary admission.

    No open/close occurs here. Retained tracking custody excludes raw offline access;
    the borrower lock prevents recovery from closing this connection underneath us.
    Check siblings too: matching our own SHM is not enough to clear a poisoned or
    mixed-generation process. All uncertain cases retain lifecycle/recovery admission.
    """
    from hermes_cli import kanban_generation as gen, kanban_session_activity as activity
    from hermes_cli.kanban_lockguard import shm_identity

    cache, cache_lock = reader_cache or (activity._RO_CACHE, activity._RO_CACHE_LOCK)
    key = str(path)
    with cache_lock:
        entry = cache.get(key)
    if entry is None or not entry[2].acquire(blocking=False):
        return None
    keep = False
    try:
        conn = entry[0]
        tracked = getattr(conn, "_hermes_tracked_path", None)
        canonical = getattr(conn, "_hermes_generation_path", None)
        if tracked is None or tracked != canonical:
            return None
        # Snapshot handles before current identity, as recover() does. A first
        # open must not be compared against a pre-open missing SHM observation.
        handles = gen._handles(canonical)
        current = shm_identity(path)
        st = os.stat(path)
        with gen._lock:
            live = conn in gen._connections and canonical not in gen._poisoned
        if (not live or conn.in_transaction or (st.st_dev, st.st_ino) != entry[1]
                or current != entry[3] or any(c._hermes_generation_shm is not None
                    and c._hermes_generation_shm != current for c in handles)):
            return None
        with cache_lock:
            keep = cache.get(key) is entry
        return (key, entry) if keep else None
    except (OSError, sqlite3.Error):
        return None
    finally:
        if not keep:
            entry[2].release()


def is_shared_reader(path, conn) -> bool:
    """Whether this borrowed connection has retained custody (not a private fallback)."""
    from hermes_cli import kanban_session_activity as activity
    with activity._RO_CACHE_LOCK:
        entry = activity._RO_CACHE.get(str(path))
        return entry is not None and entry[0] is conn


def _cached_entry_once(path, remaining, reader_cache=None):
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli.kanban_lockguard import shm_identity

    cache, cache_lock = reader_cache or (activity._RO_CACHE, activity._RO_CACHE_LOCK)
    key = str(path)
    # Same-board opens/last closes are atomic; registry locks protect only map
    # operations. Borrowers never wait for this board lock while holding the map.
    with lifecycle_access(path, timeout=remaining):
        activity._recover_generation(path)
        try:
            st = os.stat(path)
        except OSError as exc:
            raise sqlite3.OperationalError(f"unable to open database file {path}: {exc}") from exc
        ident = (st.st_dev, st.st_ino)
        with cache_lock:
            entry = cache.get(key)
            stale = entry if entry is not None and entry[1] != ident else None
            if stale is not None:
                # A borrower may need the board lock to close on a read error.
                # Never wait for it while holding that lock; preserve its entry.
                if not stale[2].acquire(blocking=False):
                    raise DeletedWalGenerationError(f"kanban DB {path}: replaced reader is still borrowed; retry")
                del cache[key]
                entry = None
        if stale is not None:
            try:
                activity._close_quietly(stale[0])
            finally:
                stale[2].release()
        if entry is None:
            conn = activity._open_ro(path, check_same_thread=False, timeout=0)
            conn.execute(f"PRAGMA busy_timeout={int(activity._RO_BUSY_TIMEOUT_S * 1000)}")
            entry = (conn, ident, threading.Lock(), shm_identity(path))
            with cache_lock:
                cache[key] = entry
        return key, entry
