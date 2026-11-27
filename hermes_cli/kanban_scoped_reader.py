"""End every snapshot when a shared read-only connection is returned. [daveey]

SQLite's in_transaction does not report an unfinished autocommit SELECT. Track
live cursors without retaining otherwise-dead results; a completed borrow owns
neither a transaction nor a cursor, even when its caller retains a traceback.
"""
import contextlib
import sqlite3
import weakref

from hermes_cli.kanban_lockguard import GuardedConnection


class ScopedReadConnection(GuardedConnection):
    def cursor(self, *args, **kwargs):
        cursor = super().cursor(*args, **kwargs)
        cursors = getattr(self, '_borrow_cursors', None)
        if cursors is None:
            cursors = self._borrow_cursors = weakref.WeakSet()
        cursors.add(cursor)
        return cursor


def close_read_quietly(conn):
    """Finalize escaped statements before close_v2 can turn the handle into a zombie."""
    for cursor in list(getattr(conn, '_borrow_cursors', ())):
        with contextlib.suppress(sqlite3.Error):
            sqlite3.Cursor.close(cursor)
    with contextlib.suppress(sqlite3.Error):
        conn.close()


def finish_cached_borrow(conn, cache, cache_lock, key, entry):
    """Caller still holds the borrower lock: finish statements before making it idle."""
    if cache.get(key) is not entry:
        return
    try:
        for cursor in list(getattr(conn, '_borrow_cursors', ())):
            # Do not invoke arbitrary subclass callbacks under the borrower lock.
            sqlite3.Cursor.close(cursor)
        if conn.in_transaction:
            conn.rollback()
    except sqlite3.Error:
        with cache_lock:
            if cache.get(key) is entry:
                del cache[key]
        close_read_quietly(conn)
