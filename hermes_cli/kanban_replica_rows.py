"""The task rows of a hermes-hub replica, read incrementally by ``seq``. [daveey]

hermes-hub stamps every replica write with its feed's monotonic ``seq``, and the replica changes every ~30 s or
faster on a busy fleet, so the remote-links memo misses all the time. Re-reading every task row on each miss
(~13k rows, ~56 MB of JSON on hermes-hub) cost ~10 s of every 90 s under ``_links_lock`` while chats waited for it.
A miss now fetches only the rows whose ``seq`` is above the last one seen, in one aggregated statement, and
checks the merged rows against the caller's probe aggregate (``count``, ``total(seq)``, ``total(version)``,
``total(octet_length(data))``). When they disagree (rows were deleted), one identity aggregate drops the vanished
keys; anything else it cannot explain (a row rewritten at an older seq, a seq that went back, a replaced file)
falls back to the full read. The result is always exactly the rows a full read returns, in ``(gw, key)`` order.

Kept rows hold each row's text zlib-compressed (``pack``), never the decoded string: the gateway, the dashboard and
every Desktop serve keep one view each, and ~56 MB of row text (UCS-4 once any row holds an emoji, so up to 4 bytes a
character) was ~115 MB of every such process's heap on the mini (t_01a1cf67a93c05b6). Equal text compresses to equal
bytes, so a kept row still compares equal exactly when its text is unchanged; ``text`` restores the text.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import zlib
from dataclasses import dataclass, field

_COLUMNS = "gw, key, data, updated_at, {version}, seq, octet_length(data)"
# Rows are read in rowid pages (all inside the caller's one read transaction, so still one snapshot) of at most
# ``_PAGE`` rows and about ``_PAGE_BYTES`` of row text at the replica's mean row size: one ``json_group_array`` over
# every row was a 256 MB string (UCS-4) plus its decode, a ~400 MB peak per full read on the mini.
_PAGE = 1000
_PAGE_BYTES = 2 << 20
_PAGE_SQL = (f"SELECT CAST(json_group_array(json_array({_COLUMNS})) AS BLOB), max(rowid) FROM (SELECT rowid, * FROM replica"
             " WHERE coll = 'tasks' AND rowid > ? {where} ORDER BY rowid LIMIT ?)")
_FULL_SQL = _PAGE_SQL.format(version="{version}", where="")
_DELTA_SQL = _PAGE_SQL.format(version="{version}", where="AND seq > ?")
# A delta is usually a few rows: read them in one query planned on the seq index (the rowid-paged form scans
# ``replica_coll`` and filters seq row by row, ~22 ms vs ~3 ms on a 14k-row hub). Only a delta bigger than a page
# (a resync rewrote many rows) is paged.
_DELTA_SIZE_SQL = "SELECT count(*), total(octet_length(data)) FROM replica WHERE coll = 'tasks' AND seq > ?"
_DELTA_ONE_SQL = f"SELECT json_group_array(json_array({_COLUMNS})) FROM replica WHERE coll = 'tasks' AND seq > ?"
_IDENTITY_SQL = "SELECT json_group_array(json_array(gw, key, seq)) FROM replica WHERE coll = 'tasks'"


@dataclass(frozen=True)
class Rows:
    """One consistent view of a replica's task rows. Immutable once returned: readers share it."""
    path: str
    ident: tuple | None  # (st_dev, st_ino) of the file the rows came from
    version_column: str
    by_key: dict[tuple, tuple]  # (gw, key) -> (gw, key, packed data, updated_at, version); ``text`` unpacks data
    meta: dict[tuple, tuple]  # (gw, key) -> (seq, octet length of data)
    sums: tuple  # (count, total seq, total integer version, total octets): ``_REPLICA_PROBE``'s terms
    last_seq: int  # the highest seq these rows had (an upper bound once rows were deleted)
    gen: int
    base_gen: int | None = None  # the view ``changed``/``removed`` are relative to (None: a full read)
    changed: frozenset = field(default_factory=frozenset)
    removed: frozenset = field(default_factory=frozenset)
    bystanders: dict = field(default_factory=dict)
    relevant: dict = field(default_factory=dict)
    me: str | None = None

    def rows(self) -> list[tuple]:
        """Raw rows for unfiltered views; filtered payloads exist only in the read snapshot.

        Use ``SnapshotRows.rows()`` instead of ``consume()`` inside that snapshot when
        raw payloads are required. Consumed scalar views deliberately cannot rehydrate.
        """
        if self.me is not None:
            raise RuntimeError('raw rows require the unconsumed replica snapshot')
        by_key = self.by_key
        return [_unpacked(by_key[k]) for k in sorted(by_key, key=_sort_key)]


def pack(data):
    """``data`` as kept: text becomes zlib-compressed ``bytes`` (never GC-tracked); anything else stays as read.
    A row read through ``json_array`` never holds ``bytes`` (JSON cannot hold a BLOB), so ``bytes`` is always packed."""
    return zlib.compress(data.encode("utf-8", "surrogatepass"), 1) if isinstance(data, str) else data


def text(data):
    """The stored text of a kept row's ``data`` (anything not packed is returned as it was read)."""
    return zlib.decompress(data).decode("utf-8", "surrogatepass") if isinstance(data, bytes) else data


def _unpacked(row: tuple) -> tuple:
    return (row[0], row[1], text(row[2]), *row[3:])


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


def page_rows(probe) -> int:
    """Rows per page for a replica whose ``_REPLICA_PROBE`` is ``probe`` (count first, total octets last)."""
    mean = float((probe[5] if probe else 0) or 0) / max(int((probe[0] if probe else 0) or 0), 1)
    return max(50, min(_PAGE, int(_PAGE_BYTES // max(mean, 1.0))))


def _paged(conn: sqlite3.Connection, sql: str, probe, args: tuple = ()):
    """Every row ``sql`` (``_FULL_SQL``/``_DELTA_SQL``) selects, one page decoded at a time."""
    from hermes_cli.kanban_replica_transport import iter_page

    size, after = page_rows(probe), -1 << 63
    encoding = conn.execute('PRAGMA encoding').fetchone()[0]
    while True:
        packed, last = conn.execute(sql, (after, *args, size)).fetchone()
        count = 0
        for row in iter_page(packed, encoding):
            count += 1
            yield row
        del packed
        if count < size:  # a short page is the last: no extra query to find the end
            return
        after = last


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


def _full(conn: sqlite3.Connection, path: str, ident, version_column: str, probe) -> Rows:
    stats["full"] += 1
    by_key, meta, sums = {}, {}, (0, 0, 0, 0)
    for gw, key, data, updated, version, seq, size in _paged(conn, _FULL_SQL.format(version=version_column), probe):
        row, m = (gw, key, pack(data), updated, version), (seq or 0, size or 0)
        by_key[(gw, key)], meta[(gw, key)] = row, m
        sums = _plus(sums, _terms(row, m))
    return Rows(path, ident, version_column, by_key, meta, sums, max((m[0] for m in meta.values()), default=0),
                _next_gen())


def _delta(conn: sqlite3.Connection, base: Rows, probe) -> Rows | None:
    """``base`` advanced to the snapshot ``probe`` describes, or None when only a full read can tell."""
    last = base.last_seq
    stats["delta"] += 1
    count, octets = conn.execute(_DELTA_SIZE_SQL, (last,)).fetchone()
    if count <= _PAGE and octets <= _PAGE_BYTES:
        fresh = _decode(conn.execute(_DELTA_ONE_SQL.format(version=base.version_column), (last,)).fetchone()[0])
    else:
        fresh = list(_paged(conn, _DELTA_SQL.format(version=base.version_column), probe, (last,)))
    stats["delta_rows"] += len(fresh)
    by_key, meta, sums = dict(base.by_key), dict(base.meta), base.sums
    changed = set()
    for gw, key, data, updated, version, seq, size in fresh:
        k = (gw, key)
        if k in by_key:
            sums = _plus(sums, _terms(by_key[k], meta[k]), -1)
        by_key[k], meta[k] = (gw, key, pack(data), updated, version), (seq or 0, size or 0)
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


def read(conn: sqlite3.Connection, path, version_column: str, probe, ident, *, me: str | None = None):
    """The task rows in ``conn``'s open read transaction. ``probe`` is ``_REPLICA_PROBE`` read in that same
    transaction, ``ident`` the file identity taken before ``conn`` was opened. The caller reads here only a replica
    whose every task row carries a ``seq`` (``probe[0] == probe[1]``) and that has a ``version`` column."""
    if me is not None:
        from hermes_cli.kanban_replica_scalar_rows import SnapshotRows
        return SnapshotRows(conn, path, version_column, probe, ident, me)
    tag = str(path)
    if ident is None:  # no file identity, so no base to trust
        return _full(conn, tag, None, version_column, probe)
    with _lock:
        base = _views.get(tag)
    view = None
    if base is not None and base.ident == ident and base.me is None and base.version_column == version_column:
        view = _delta(conn, base, probe)
    if view is None:
        view = _full(conn, tag, ident, version_column, probe)
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
