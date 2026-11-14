"""Bounded guarded replica readers for dispatcher scopes that consume all results.

Unlike foreign_card.open_replica, callers must not retain the connection or a cursor
past the scope. No results are cached: external commits remain visible every tick.
"""
from contextlib import contextmanager, ExitStack, closing
import logging
import sqlite3

from hermes_cli import kanban_foreign_card as foreign
from hermes_cli import kanban_reader_retention as retention

_log = logging.getLogger(__name__)


def _discard(key, entry):
    from hermes_cli import kanban_session_activity as activity

    with activity._RO_CACHE_LOCK:
        if activity._RO_CACHE.get(key) is entry:
            del activity._RO_CACHE[key]
    activity._close_quietly(entry[0])


@contextmanager
def _borrow(path):
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli.kanban_reader_lifecycle import cached_entry
    from hermes_cli.kanban_generation import DeletedWalGenerationError

    key, entry = cached_entry(path, busy_budget=5)
    conn, _ident, lock, _shm = entry
    # Shared readers also serve unrelated callers. Never deadlock a nested borrow
    # or wait behind a slow body: retain the old independent-reader path instead.
    if not lock.acquire(blocking=False):
        with closing(foreign._ro(path)) as transient:
            yield transient
        return
    try:
        with activity._RO_CACHE_LOCK:
            if activity._RO_CACHE.get(key) is not entry:
                raise DeletedWalGenerationError(f"kanban DB {path}: reader evicted before borrow")
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=5000')
            yield conn
        except sqlite3.Error:
            _discard(key, entry)
            raise
        finally:
            with activity._RO_CACHE_LOCK:
                retained = activity._RO_CACHE.get(key) is entry
            if retained:
                try:
                    # Even a non-SQLite body exception must release its snapshot.
                    conn.rollback()
                    conn.row_factory = None
                    conn.execute(f'PRAGMA busy_timeout={int(activity._RO_BUSY_TIMEOUT_S * 1000)}')
                except sqlite3.Error:
                    _discard(key, entry)
    finally:
        lock.release()
        retention.prune()


@contextmanager
def open_replica(db_path):
    """Resolve sync policy each time; an unavailable replica is a miss, not a create.

    Only acquisition errors become None. Errors in the caller's body propagate.
    """
    from hermes_cli.kanban_generation import DeletedWalGenerationError

    path = foreign.replica_path(db_path)
    with ExitStack() as stack:
        conn = None
        if path is not None:
            for attempt in range(2):
                try:
                    conn = stack.enter_context(_borrow(path))
                    break
                except sqlite3.Error as exc:
                    if attempt or not isinstance(exc, (sqlite3.ProgrammingError, DeletedWalGenerationError)):
                        _log.info('kanban: replica %s unreadable (%s)', path, exc)
                        break
        yield conn
