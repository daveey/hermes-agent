"""Commit-keyed structural notice tails; callers apply clock predicates each tick."""
from collections import OrderedDict
import threading
from typing import Any, cast

_cache: OrderedDict[tuple, tuple] = OrderedDict()
_lock = threading.Lock()
_MAX_READERS = 32


def rows(db: Any, sql: str) -> list[Any]:
    """Reuse rows only on the same reader and committed revision, never in a transaction."""
    read = getattr(db, '_read_retrying_ioerr', None)
    if not callable(read):
        return db._read_all(sql)

    def probe(conn):
        key = (db, conn, sql)
        if conn.in_transaction:
            with _lock:
                _cache.pop(key, None)
            return conn.execute(sql).fetchall()
        stamp = (conn.execute('PRAGMA data_version').fetchone()[0], conn.total_changes)
        with _lock:
            cached = _cache.get(key)
            if cached is not None and cached[0] == stamp:
                _cache.move_to_end(key)
                return list(cached[1])
            _cache.pop(key, None)
        result = conn.execute(sql).fetchall()
        # A concurrent commit after the stamp is observed on the very next call.
        with _lock:
            _cache[key] = (stamp, tuple(result))
            while len(_cache) > _MAX_READERS:
                _cache.popitem(last=False)
        return result

    return cast(list[Any], read(probe))
