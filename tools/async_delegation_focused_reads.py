"""Commit-keyed, read-only durable failure rows for focused session polling.

Only immutable SQL rows are retained, never owner authority or live observations.
The caller evaluates its time window on every read (including clock rollback).
"""
import sqlite3
import threading
from collections import OrderedDict

_readers = threading.local()
_MAX_HOMES = 8
_MAX_SCOPES = 32


def _identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _query(conn, selectors, limit):
    owner_sql = ' OR '.join(f'{col}=?' for col, _ in selectors)
    return tuple(conn.execute(
        "SELECT delegation_id, state, dispatched_at, completed_at, task_json, result_json "
        f"FROM async_delegations WHERE ({owner_sql}) AND state NOT IN ('running','finalizing') "
        "AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT ?",
        (*(val for _, val in selectors), limit)).fetchall())


def failure_rows(path, selectors, limit):
    """No writer/bootstrap on polls; errors drop the reader and retry next call.

    data_version belongs to this retained connection, not another handle. Read it
    BEFORE querying so a concurrent commit cannot certify older rows as current.
    Bound both connections and scope entries; expiry never relies on a cache TTL.
    """
    readers = getattr(_readers, 'entries', None)
    if readers is None:
        readers = _readers.entries = OrderedDict()
    key = str(path.absolute())
    try:
        identity = _identity(path)
        entry = readers.get(key)
        if entry is not None and entry[0] != identity:
            readers.pop(key)[1].close()
            entry = None
        if entry is None:
            conn = sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=0.1)
            entry = (identity, conn, OrderedDict())
            readers[key] = entry
            if _identity(path) != identity:
                raise OSError('ledger replaced during open')
            while len(readers) > _MAX_HOMES:
                readers.popitem(last=False)[1][1].close()
        readers.move_to_end(key)
        conn, scopes = entry[1:]
        revision = conn.execute('PRAGMA data_version').fetchone()[0]
        scope = (tuple(selectors), limit)
        cached = scopes.get(scope)
        if cached is None or cached[0] != revision:
            rows = _query(conn, selectors, limit)
            if _identity(path) != identity:
                raise OSError('ledger replaced during read')
            scopes[scope] = (revision, rows)
        scopes.move_to_end(scope)
        while len(scopes) > _MAX_SCOPES:
            scopes.popitem(last=False)
        return scopes[scope][1]
    except (OSError, sqlite3.Error):
        if entry := readers.pop(key, None):
            entry[1].close()
        return ()
