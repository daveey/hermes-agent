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


def cached_entry(path, busy_budget=0.5):
    return retry_open(lambda remaining: _cached_entry_once(path, remaining), busy_budget)


def _cached_entry_once(path, remaining):
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli.kanban_lockguard import shm_identity

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
        with activity._RO_CACHE_LOCK:
            entry = activity._RO_CACHE.get(key)
            stale = entry if entry is not None and entry[1] != ident else None
            if stale is not None:
                # A borrower may need the board lock to close on a read error.
                # Never wait for it while holding that lock; preserve its entry.
                if not stale[2].acquire(blocking=False):
                    raise DeletedWalGenerationError(f"kanban DB {path}: replaced reader is still borrowed; retry")
                del activity._RO_CACHE[key]
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
            with activity._RO_CACHE_LOCK:
                activity._RO_CACHE[key] = entry
        return key, entry
