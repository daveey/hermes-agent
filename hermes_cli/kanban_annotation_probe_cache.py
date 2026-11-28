"""Bounded successful revision probes, fenced by reader and SQLite file generation."""
from __future__ import annotations

import threading
from collections import OrderedDict

from hermes_cli.kanban_annotation_watch import signature

_lock = threading.Lock()
_cache: OrderedDict = OrderedDict()
_CAP = 128


def read(conn, path, identity, kinds, reader):
    """Reuse only an unchanged, committed source on the same managed reader.

    Caller owns the reader and the per-path probe lock. Never memoize a caller's
    transaction: its snapshot can predate the current filesystem signature.
    The safe header reader observes same-size/same-mtime WAL commits too.
    """
    if conn.in_transaction:
        return reader(conn, identity, kinds)
    raw = signature((), [str(path)], [str(path) + '-shm'])
    key = (conn, identity, tuple(k.name for k in kinds))
    with _lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == raw:
            _cache.move_to_end(key)
            return dict(hit[1])
    values = reader(conn, identity, kinds)
    if all(value is not None for value in values.values()) and raw == signature((), [str(path)], [str(path) + '-shm']):
        with _lock:
            _cache[key] = (raw, dict(values))
            _cache.move_to_end(key)
            while len(_cache) > _CAP:
                _cache.popitem(last=False)
    return values
