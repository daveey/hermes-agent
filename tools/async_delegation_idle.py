"""Commit-keyed empty proofs for the async orphan sweep. [daveey]

Only a ledger with no running/finalizing or pending rows is quiet. Pending rows
are never cached: owner death, delivery leases and replay age change with time.
Readers are thread-local, bounded, read-only, and checked for file replacement.
"""
import sqlite3
import threading
from collections import OrderedDict

_readers = threading.local()


def _has_work(conn):
    return conn.execute(
        "SELECT 1 FROM async_delegations WHERE state IN ('running','finalizing') "
        "OR delivery_state='pending' LIMIT 1"
    ).fetchone() is not None


def ledger_quiet(path):
    """Fail open on missing/unreadable stores; never create or migrate a ledger."""
    readers = getattr(_readers, "entries", None)
    if readers is None:
        readers = _readers.entries = OrderedDict()
    key = str(path)
    try:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        entry = readers.get(key)
        if entry is not None and entry[0] != identity:
            readers.pop(key)[1].close()
            entry = None
        if entry is None:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
            entry = [identity, conn, None]
            readers[key] = entry
            while len(readers) > 32:
                readers.popitem(last=False)[1][1].close()
        readers.move_to_end(key)
        conn = entry[1]
        revision = conn.execute("PRAGMA data_version").fetchone()[0]
        if entry[2] == revision:
            return True
        entry[2] = None
        if not _has_work(conn):
            entry[2] = revision
            return True
    except (OSError, sqlite3.Error):
        if entry := readers.pop(key, None):
            entry[1].close()
    return False
