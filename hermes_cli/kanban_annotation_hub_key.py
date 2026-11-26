"""Outer annotation-only heartbeat equivalence with whole-pass commit custody.

Inner authoritative fingerprints remain literal. Any replica commit across a
read refuses certification, including unrelated/session commits. This deliberately
conservative fence must not be replaced by semantic endpoint equality.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from hermes_cli import kanban_scope_heartbeat as hb
from hermes_cli.kanban_annotation_reader import _file_fence


def _stamp(conn, path):
    if conn.in_transaction:
        raise ValueError('publication fence requires an unpinned connection')
    return (_file_fence(path), conn.execute('PRAGMA data_version').fetchone()[0],
            conn.total_changes, conn.execute('PRAGMA schema_version').fetchone()[0])


@dataclass(frozen=True)
class Receipt:
    path: Path
    conn: sqlite3.Connection
    stamp: tuple

    def valid(self):
        from hermes_cli import kanban_session_activity as ksa

        try:
            with ksa.cached_ro(self.path, busy_budget=0) as conn:
                return conn is self.conn and _stamp(conn, self.path) == self.stamp
        except (sqlite3.Error, OSError, ValueError):
            return False


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON member')
        value[key] = item
    return value


def _gateway(gw, key, seq, raw, now):
    literal = ('literal', gw, key, seq, raw)
    if key != 'info':
        return literal
    try:
        info = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(info, dict) or info.get('gateway') != gw:
            return literal
        scopes = info.get('task_scopes')
        if not isinstance(scopes, list):
            return literal
        identities = [(s.get('profile'), s.get('board')) for s in scopes if isinstance(s, dict)
                      and isinstance(s.get('profile'), str) and isinstance(s.get('board'), str)]
        counts = {ident: identities.count(ident) for ident in identities}
        # Typed outer structure makes a normalized leaf unlike any literal JSON value.
        normalized = []
        changed = False
        for scope in scopes:
            if not isinstance(scope, dict):
                normalized.append(('literal', scope))
                continue
            ident = scope.get('profile'), scope.get('board')
            eligible = (isinstance(ident[0], str) and isinstance(ident[1], str)
                        and counts.get(ident) == 1 and scope.get('state') == 'ok'
                        and hb.fresh(scope.get('observed_at'), now, hb.scope_window(info)))
            normalized.append(('fresh', {k: v for k, v in scope.items() if k != 'observed_at'})
                              if eligible else ('literal', scope))
            changed |= eligible
        if not changed:
            return literal
        outer = {k: v for k, v in info.items() if k != 'task_scopes'}
        return ('normalized', gw, key, json.dumps([outer, normalized], sort_keys=True, allow_nan=False,
                                                separators=(',', ':')))
    except (TypeError, ValueError, OverflowError, RecursionError):
        return literal


def _counts_meta(rows, now):
    """Counts discard raw sync provenance; Cards/links emphatically do not.

    Keep unknown metadata literal. These known pull/staging counters are not
    selected by either remote projection. Preserve both readers' freshness
    semantics by normalizing ONLY their joint finite, nonfuture fresh region,
    with no offline row. Existing clock deadlines and renewal remain owned by
    actual reads, never refreshed by this probe.
    """
    bookkeeping = {'cursor', 'head', 'watch_last_event'}
    offline = any(row[0] == 'offline_since' for row in rows)
    out = []
    for key, kind, value in rows:
        if key in bookkeeping or key.startswith('staged_at:'):
            continue
        if key == 'last_ok' and not offline and kind in ('text', 'real', 'integer'):
            try:
                last_ok = float(value)
                if math.isfinite(last_ok) and last_ok > 0 and 0 <= now - last_ok <= 180:
                    out.append(('counts-fresh-last-ok',))
                    continue
            except (TypeError, ValueError, OverflowError):
                pass
        out.append(('literal', key, kind, value))
    return tuple(out)


def capture(path: Path, now: float | None = None):
    """One semantic snapshot and its original raw receipt; never bless a later stamp."""
    from hermes_cli import kanban_session_activity as ksa

    now = time.time() if now is None else now
    with ksa.cached_ro(path, busy_budget=0) as conn:
        start = _stamp(conn, path)
        conn.execute('BEGIN')
        try:
            tasks = tuple(tuple(r) for r in conn.execute(
                "SELECT coll,COUNT(*),MAX(seq),TOTAL(seq) FROM replica WHERE coll='tasks' GROUP BY coll"))
            gateways = tuple(_gateway(*r, now) for r in conn.execute(
                "SELECT gw,key,seq,data FROM replica WHERE coll='gateway' ORDER BY gw,key"))
            meta = tuple(tuple(r) for r in conn.execute("SELECT k,typeof(v),v FROM meta ORDER BY k"))
        finally:
            conn.rollback()
        if _stamp(conn, path) != start:
            raise ValueError('replica changed while acquiring annotation key')
        return (str(path), start[0], tasks, gateways, _counts_meta(meta, now)), Receipt(path, conn, start)
