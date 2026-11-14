"""Snapshot-scoped row consumption with payload-free bystander evidence. [daveey]"""
from __future__ import annotations

import hashlib
import json
import marshal
from collections.abc import Mapping

from hermes_cli.kanban_replica_relevance import is_bystander

# Scalars are process-local, generated here, never loaded from a database as marshal.
# seq, octets, version, updated, created, text digest, terminal/verified flags


class ScalarRows(Mapping):
    """All-identity compatibility view; only relevant rows have payload slots."""
    def __init__(self, relevant, bystanders, *, meta=False):
        self.relevant, self.bystanders, self.meta = relevant, bystanders, meta

    def __len__(self):
        return len(self.relevant) + len(self.bystanders)

    def __iter__(self):
        yield from self.relevant
        yield from self.bystanders

    def __getitem__(self, key):
        if key in self.relevant:
            return self.relevant[key]
        seq, size, version, updated, *_ = marshal.loads(self.bystanders[key])
        return (seq, size) if self.meta else (*key, b'', updated, version)


def derive(raw, me, legacy):
    """Return derived scalar bytes, or None for a relevant/invalid record."""
    from hermes_cli.kanban_session_activity import _decode_replica_task
    from hermes_cli.kanban_session_activity_release import record_attests_terminal

    gw, key, data, updated, version, seq, size = raw
    try:
        task, _, evidence = _decode_replica_task(key, data)
        if not is_bystander(task, key, me, legacy.get(gw, ())):
            return None
        profile, board, tid = key.split('/')
        if tid != str(task['id']):
            return None
    except ValueError:
        return None  # the ordinary stage retains sorted malformed-row errors
    # Preserve eager aggregate errors rather than moving them to a later facade probe.
    (updated or 0) / 1000.0
    created = task.get('created_at')
    if created is not None:
        hash((board, tid, created))
    attest = record_attests_terminal(task, str(gw), profile, board, evidence)
    flags = int(attest) | (2 if attest and evidence.get('verified') is True else 0)
    digest = hashlib.blake2b(data.encode('utf-8', 'surrogatepass'), digest_size=16).digest()
    return marshal.dumps((seq or 0, size or 0, version, updated, created, digest, flags))


class SnapshotRows:
    """Single-use adapter; the caller keeps the connection open until consumption."""
    def __init__(self, conn, path, version_column, probe, ident, me):
        self.conn, self.path, self.version_column = conn, str(path), version_column
        self.probe, self.ident, self.me = probe, ident, me

    def _take(self):
        conn, self.conn = self.conn, None
        if conn is None:
            raise RuntimeError('replica snapshot already consumed')
        return conn

    def rows(self):
        from hermes_cli import kanban_replica_rows as r
        conn = self._take()
        return sorted((row[:5] for row in r._paged(conn, r._FULL_SQL.format(version=self.version_column), self.probe)),
                      key=lambda row: r._sort_key(row[:2]))

    def consume(self):
        from hermes_cli import kanban_replica_rows as r
        conn = self._take()
        packed = conn.execute("SELECT json_group_array(json_array(gw,key)) FROM replica WHERE coll='tasks' "
                              "AND length(key)-length(replace(key,'/',''))=1").fetchone()[0]
        legacy = {}
        for gw, key in json.loads(packed):
            legacy.setdefault(gw, set()).add(key)
        with r._lock:
            base = r._views.get(self.path)
        if base is not None and (self.ident is None or base.ident != self.ident or base.me != self.me
                                 or base.version_column != self.version_column):
            base = None
        builder = _Builder(self, legacy, base)
        view = builder.delta(conn) if base is not None else None
        if view is None:
            builder = _Builder(self, legacy, base, full=True)
            r.stats['full'] += 1
            if self.probe[0] <= r._PAGE and self.probe[5] <= r._PAGE_BYTES:
                sql = r._DELTA_ONE_SQL.format(version=self.version_column).removesuffix(' AND seq > ?')
                fresh = r._decode(conn.execute(sql).fetchone()[0])
            else:
                fresh = r._paged(conn, r._FULL_SQL.format(version=self.version_column), self.probe)
            for raw in fresh:
                builder.put(raw)
            view = builder.finish()
        if self.ident is not None:
            with r._lock:
                if len(r._views) >= 16 and self.path not in r._views:
                    r._views.pop(next(iter(r._views)))
                r._views[self.path] = view
        return view


class _Builder:
    """Private mutable candidate; only a reconciled generation is published."""
    def __init__(self, snapshot, legacy, base, full=False):
        self.snapshot, self.legacy, self.base, self.full = snapshot, legacy, base, full
        keep = base is not None and not full
        self.relevant = dict(base.relevant) if keep else {}
        self.meta = dict(base.meta.relevant) if keep else {}
        self.scalars = dict(base.bystanders) if keep else {}
        self.rows = ScalarRows(self.relevant, self.scalars)
        self.metadata = ScalarRows(self.meta, self.scalars, meta=True)
        self.sums = base.sums if keep else (0, 0, 0, 0)
        self.top = base.last_seq if keep else 0
        self.changed, self.removed, self.gateways = set(), set(), {}
        self.projected = 0

    def drop(self, rk):
        from hermes_cli import kanban_replica_rows as r
        self.sums = r._plus(self.sums, r._terms(self.rows[rk], self.metadata[rk]), -1)
        self.relevant.pop(rk, None)
        self.meta.pop(rk, None)
        self.scalars.pop(rk, None)

    def put(self, raw):
        from hermes_cli import kanban_replica_rows as r
        gw, key, data, updated, version, seq, size = raw
        gw = self.gateways.setdefault(gw, gw)
        rk, m = (gw, key), (seq or 0, size or 0)
        scalar = derive(raw, self.snapshot.me, self.legacy)
        if rk in self.rows:
            self.drop(rk)
        if scalar is None:
            self.relevant[rk], self.meta[rk] = (gw, key, r.pack(data), updated, version), m
        else:
            self.scalars[rk] = scalar
            old = self.base.bystanders.get(rk) if self.base is not None else None
            if old is None or marshal.loads(old)[2:6] != marshal.loads(scalar)[2:6]:
                self.projected += 1
        self.sums = r._plus(self.sums, (1, m[0], version if type(version) is int else 0, m[1]))
        self.top = max(self.top, m[0])
        if not self.full:
            self.changed.add(rk)

    def delta(self, conn):
        from hermes_cli import kanban_replica_rows as r
        from hermes_cli.kanban_replica_relevance import fetch_payloads
        base = self.base
        r.stats['delta'] += 1
        count, octets = conn.execute(r._DELTA_SIZE_SQL, (base.last_seq,)).fetchone()
        if count <= r._PAGE and octets <= r._PAGE_BYTES:
            fresh = r._decode(conn.execute(r._DELTA_ONE_SQL.format(version=base.version_column),
                                          (base.last_seq,)).fetchone()[0])
        else:
            fresh = r._paged(conn, r._DELTA_SQL.format(version=base.version_column), self.snapshot.probe,
                             (base.last_seq,))
        for raw in fresh:
            r.stats['delta_rows'] += 1
            self.put(raw)
        if not r._matches(self.sums, self.snapshot.probe):
            r.stats['identity'] += 1
            present = {(gw, key): seq for gw, key, seq in r._decode(conn.execute(r._IDENTITY_SQL).fetchone()[0])}
            self.removed = set(self.rows) - present.keys()
            for rk in self.removed:
                self.drop(rk)
            if any(present.get(rk) != m[0] for rk, m in self.metadata.items()):
                return None
            if not r._matches(self.sums, self.snapshot.probe):
                return None
        promotions = [rk for rk in self.scalars if rk[1].split('/', 1)[1] in self.legacy.get(rk[0], ())]
        for raw in fetch_payloads(conn, promotions, base.version_column):
            self.put(raw)
        # An unchanged qualified twin may lose its legacy alias; classify its kept text.
        for rk in list(self.relevant):
            if (rk not in self.changed and rk[1].count('/') == 2
                    and (rk[0], rk[1].split('/', 1)[1]) in self.removed):
                row, m = self.relevant[rk], self.meta[rk]
                raw = (*row[:2], r.text(row[2]), *row[3:], *m)
                if derive(raw, self.snapshot.me, self.legacy) is not None:
                    self.put(raw)
        return self.finish()

    def finish(self):
        from hermes_cli import kanban_replica_rows as r
        from hermes_cli import kanban_replica_projection as p
        p.stats['projected'] += self.projected
        s = self.snapshot
        return r.Rows(s.path, s.ident, s.version_column, self.rows, self.metadata, self.sums, self.top,
                      r._next_gen(), None if self.full or self.base is None else self.base.gen,
                      frozenset(self.changed), frozenset(self.removed), self.scalars, self.relevant, s.me)
