"""Successful links revision probes on unchanged managed SQLite readers."""
from collections import OrderedDict
import threading

from hermes_cli.kanban_source_signature import signature

_lock = threading.Lock()
_cache: OrderedDict = OrderedDict()
_CAP = 128


def read(conn, path, identity, tables, reader):
    """Caller owns the path lock and connection; unknown coverage always retries.

    Snapshot-owned callers bypass reuse: their snapshot may predate the file
    token. File/header probes use the existing lock-safe SQLite watcher.
    """
    if conn.in_transaction:
        return reader(conn, path, identity, tables)
    raw = signature((), [str(path)], [str(path) + '-shm'])
    if raw is None:
        return reader(conn, path, identity, tables)
    key = (conn, str(path), identity, tables)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == raw:
            _cache.move_to_end(key)
            return hit[1]
    value = reader(conn, path, identity, tables)
    if value is not None and raw == signature((), [str(path)], [str(path) + '-shm']):
        with _lock:
            _cache[key] = (raw, value)
            _cache.move_to_end(key)
            while len(_cache) > _CAP:
                _cache.popitem(last=False)
    return value
