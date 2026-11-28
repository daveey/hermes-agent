"""Change-keyed routing data for outcome polling; never cache turn liveness."""
from collections import OrderedDict
import json
import sqlite3
import threading
from typing import Any


_LOCAL = threading.local()
_MAX_CONNECTIONS = 8


def active_routing_entries(db: Any, sid: str) -> tuple:
    """Reuse parsed routing rows only on the exact DB/connection and SQLite revision.

    Readers are checked out exclusively by SessionDB. A thread-local bounded LRU
    avoids a global lock and retains connection objects (not recyclable ids).
    Writer fallback transactions bypass and evict the cache: total_changes does
    not change on rollback, so publishing uncommitted rows would poison it.
    """
    def read(conn):
        cache = getattr(_LOCAL, "snapshots", None)
        if cache is None:
            cache = _LOCAL.snapshots = OrderedDict()
        identity = (db, conn)
        if conn.in_transaction:
            cache.pop(identity, None)
            return _scan(conn).get(sid, ())
        revision = _revision(conn)
        cached = cache.get(identity)
        if cached is not None and cached[0] == revision:
            cache.move_to_end(identity)
            return cached[1].get(sid, ())
        cache.pop(identity, None)
        grouped = _scan(conn)
        # A concurrent commit between PRAGMA and SELECT must not label an older
        # scan with a newer revision. Return this read, but don't publish it.
        if _revision(conn) == revision and not conn.in_transaction:
            cache[identity] = (revision, grouped)
            while len(cache) > _MAX_CONNECTIONS:
                cache.popitem(last=False)
        return grouped.get(sid, ())

    return db._read_retrying_ioerr(read)


def _revision(conn):
    return conn.execute("PRAGMA data_version").fetchone()[0], conn.total_changes


def _scan(conn):
    from hermes_state_routing_index import ACTIVE_ROUTING_INDEX, ACTIVE_ROUTING_PREDICATE

    grouped = {}
    select = "SELECT scope, entry_json, json_extract(entry_json, '$.session_id') AS sid FROM gateway_routing"
    where = " WHERE json_extract(entry_json, '$.active_turn_token') IS NOT NULL"
    try:
        rows = conn.execute(select + f" INDEXED BY {ACTIVE_ROUTING_INDEX}" + where
                            + " AND (" + ACTIVE_ROUTING_PREDICATE + ")").fetchall()
    except sqlite3.OperationalError as exc:
        # Only absence of our optional index permits literal legacy/read-only
        # fallback. Malformed JSON, busy, corruption and unknown layouts raise.
        if str(exc) != f"no such index: {ACTIVE_ROUTING_INDEX}":
            raise
        rows = conn.execute(select + where).fetchall()
    for row in rows:
        try:
            entry = json.loads(row["entry_json"] or "{}")
        except ValueError:
            continue
        grouped.setdefault(row["sid"], []).append((row["scope"], entry))
    return {sid: tuple(entries) for sid, entries in grouped.items()}
