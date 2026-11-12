"""The task rows of a hermes-hub replica, read incrementally by ``seq``. [daveey]

hermes-hub stamps every replica write with its feed's monotonic ``seq``, and the replica changes every ~30 s or
faster on a busy fleet, so the remote-links memo misses all the time. Re-reading every task row on each miss
(~13k rows, ~56 MB of JSON on hermes-hub) cost ~10 s of every 90 s under ``_links_lock`` while chats waited for it.
A miss now fetches only the rows whose ``seq`` is above the last one seen, in one aggregated statement, and
checks the merged rows against the caller's probe aggregate (``count``, ``total(seq)``, ``total(version)``,
``total(octet_length(data))``). When they disagree (rows were deleted), one identity aggregate drops the vanished
keys; anything else it cannot explain (a row rewritten at an older seq, a seq that went back, a replaced file)
falls back to the full read. The result is always exactly the rows a full read returns, in ``(gw, key)`` order.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass, field

_COLUMNS = "gw, key, data, updated_at, {version}, seq, octet_length(data)"
_FULL_SQL = f"SELECT json_group_array(json_array({_COLUMNS})) FROM replica WHERE coll = 'tasks'"
_DELTA_SQL = _FULL_SQL + " AND seq > ?"
_IDENTITY_SQL = "SELECT json_group_array(json_array(gw, key, seq)) FROM replica WHERE coll = 'tasks'"


@dataclass(frozen=True)
class Rows:
    """One consistent view of a replica's task rows. Immutable once returned: readers share it."""
    path: str
    ident: tuple | None  # (st_dev, st_ino) of the file the rows came from
    version_column: str
    by_key: dict[tuple, tuple]  # (gw, key) -> (gw, key, data, updated_at, version), as ``_read_remote_links`` reads
    meta: dict[tuple, tuple]  # (gw, key) -> (seq, octet length of data)
    sums: tuple  # (count, total seq, total integer version, total octets): ``_REPLICA_PROBE``'s terms
    last_seq: int  # the highest seq these rows had (an upper bound once rows were deleted)
    gen: int
    base_gen: int | None = None  # the view ``changed``/``removed`` are relative to (None: a full read)
    changed: frozenset = field(default_factory=frozenset)
    removed: frozenset = field(default_factory=frozenset)

    def rows(self) -> list[tuple]:
        by_key = self.by_key
        return [by_key[k] for k in sorted(by_key, key=_sort_key)]


_lock = threading.Lock()
_views: dict[str, Rows] = {}
_gen = [0]
# Work counters, for tests that pin "changed rows only" without timing anything.
stats = {"full": 0, "delta": 0, "delta_rows": 0, "identity": 0}


def file_ident(path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _next_gen() -> int:
    with _lock:
        _gen[0] += 1
        return _gen[0]


def _decode(packed: str) -> list[tuple]:
    return [tuple(item) for item in json.loads(packed)]


def _sort_key(key: tuple) -> tuple:
    return str(key[0]), str(key[1])


def _terms(row: tuple, meta: tuple) -> tuple:
    version = row[4]
    return 1, meta[0], version if type(version) is int else 0, meta[1]


def _plus(a: tuple, b: tuple, sign: int = 1) -> tuple:
    return tuple(x + sign * y for x, y in zip(a, b))


def _matches(sums: tuple, probe) -> bool:
    return (sums[0] == probe[0] and float(sums[1]) == float(probe[3] or 0)
            and float(sums[2]) == float(probe[4] or 0) and float(sums[3]) == float(probe[5] or 0))


def _full(conn: sqlite3.Connection, path: str, ident, version_column: str) -> Rows:
    stats["full"] += 1
    by_key, meta, sums = {}, {}, (0, 0, 0, 0)
    for gw, key, data, updated, version, seq, size in _decode(
            conn.execute(_FULL_SQL.format(version=version_column)).fetchone()[0]):
        row, m = (gw, key, data, updated, version), (seq or 0, size or 0)
        by_key[(gw, key)], meta[(gw, key)] = row, m
        sums = _plus(sums, _terms(row, m))
    return Rows(path, ident, version_column, by_key, meta, sums, max((m[0] for m in meta.values()), default=0),
                _next_gen())


def _delta(conn: sqlite3.Connection, base: Rows, probe) -> Rows | None:
    """``base`` advanced to the snapshot ``probe`` describes, or None when only a full read can tell."""
    last = base.last_seq
    stats["delta"] += 1
    fresh = _decode(conn.execute(_DELTA_SQL.format(version=base.version_column), (last,)).fetchone()[0])
    stats["delta_rows"] += len(fresh)
    by_key, meta, sums = dict(base.by_key), dict(base.meta), base.sums
    changed = set()
    for gw, key, data, updated, version, seq, size in fresh:
        k = (gw, key)
        if k in by_key:
            sums = _plus(sums, _terms(by_key[k], meta[k]), -1)
        by_key[k], meta[k] = (gw, key, data, updated, version), (seq or 0, size or 0)
        sums = _plus(sums, _terms(by_key[k], meta[k]))
        changed.add(k)
    removed: set = set()
    if not _matches(sums, probe):
        stats["identity"] += 1
        present = {(gw, key): seq for gw, key, seq in _decode(conn.execute(_IDENTITY_SQL).fetchone()[0])}
        removed = set(by_key) - present.keys()
        for k in removed:
            sums = _plus(sums, _terms(by_key.pop(k), meta.pop(k)), -1)
        # A kept row whose seq moved without passing ``last`` (rewritten at an older seq, a resync, another
        # file's rows): only a full read can tell what it holds. Then the totals must add up: a key we never saw
        # shows in the count, and a row rewritten in place keeps its seq but not, as a rule, its version and length.
        if any(present.get(k) != m[0] for k, m in meta.items()):
            return None
        if not _matches(sums, probe):
            return None
    top = max((meta[k][0] for k in changed), default=last)
    return Rows(base.path, base.ident, base.version_column, by_key, meta, sums, top, _next_gen(),
                base.gen, frozenset(changed), frozenset(removed))


def read(conn: sqlite3.Connection, path, version_column: str, probe, ident) -> Rows:
    """The task rows in ``conn``'s open read transaction. ``probe`` is ``_REPLICA_PROBE`` read in that same
    transaction, ``ident`` the file identity taken before ``conn`` was opened. The caller reads here only a replica
    whose every task row carries a ``seq`` (``probe[0] == probe[1]``) and that has a ``version`` column."""
    tag = str(path)
    if ident is None:  # no file identity, so no base to trust
        return _full(conn, tag, None, version_column)
    with _lock:
        base = _views.get(tag)
    view = None
    if base is not None and base.ident == ident:
        view = _delta(conn, base, probe)
    if view is None:
        view = _full(conn, tag, ident, version_column)
    with _lock:
        if len(_views) >= 16 and tag not in _views:
            _views.pop(next(iter(_views)))
        _views[tag] = view
    return view


def reset() -> None:
    with _lock:
        _views.clear()
    for k in stats:
        stats[k] = 0
