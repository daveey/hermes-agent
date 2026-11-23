"""Invocation-local containment evidence for wait display, never lifecycle authority.

One read snapshot captures selected histories and their receipt/attestation inputs.
Nothing survives the display invocation, so the next board revision cannot reuse
an old verdict; strict readers and SQLite guard functions retain literal reads.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from hermes_cli.kanban_containment_state import _payload, _stop, fingerprint, reduce_events


class Capture:
    def __init__(self, conn, task_ids):
        cursor = conn.execute(
            "SELECT uid,task_id,kind,payload,created_at FROM task_events "
            "WHERE task_id IN (SELECT value FROM json_each(?)) "
            "AND kind IN ('contained','containment_released','containment_closed')", (json.dumps(task_ids),))
        names = [c[0] for c in cursor.description]
        self.events = {}
        for row in cursor:
            event = dict(zip(names, row))
            self.events.setdefault(str(event['task_id']), []).append(event)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.board = None
        row = None
        if 'kanban_sync_state' in tables:
            row = conn.execute("SELECT board_id FROM kanban_sync_state LIMIT 1").fetchone()
            self.board = row[0] if row else None
        self.synced = 'kanban_sync_state' in tables and row is not None
        self.evidence = {}
        self.accepted = {}
        self.attestations = {}

        uids = json.dumps([e['uid'] for events in self.events.values() for e in events])
        if self.synced and 'containment_canonical_receipts' in tables:
            self.evidence = dict(conn.execute(
                "SELECT uid,evidence FROM containment_canonical_receipts "
                "WHERE uid IN (SELECT value FROM json_each(?))", (uids,)))
            if 'containment_sync_acceptances' in tables:
                # Newest acceptance wins, exactly as accepted_evidence (including rowid ties).
                for uid, raw in conn.execute(
                        "SELECT receipt_uid,evidence FROM containment_sync_acceptances "
                        "WHERE receipt_uid IN (SELECT value FROM json_each(?)) "
                        "ORDER BY accepted_at,rowid", (uids,)):
                    self.accepted[uid] = raw
        elif not self.synced and 'containment_attestations' in tables:
            self.attestations = {r[0]: tuple(r[1:]) for r in conn.execute(
                "SELECT receipt_uid,task_id,episode_uid,receipt_sha256,episode_sha256 "
                "FROM containment_attestations WHERE receipt_uid IN (SELECT value FROM json_each(?))", (uids,))}

    def verify(self, receipt, stop):
        if self.synced:
            from hermes_cli.kanban_containment_replica import verified_evidence
            # Decode only on demand: malformed unused evidence must remain irrelevant.
            return verified_evidence(receipt, stop, self._cached(stop.get('uid')),
                                     self._cached(receipt.get('uid')), self.board)
        data, frozen = _payload(receipt), _stop(stop)
        if data is None or frozen is None:
            return False
        return self.attestations.get(receipt.get('uid')) == (
            receipt.get('task_id'), stop.get('uid'), fingerprint(data), fingerprint(frozen))

    def _cached(self, uid):
        if uid in self.accepted:
            newer = json.loads(self.accepted[uid])
            if newer is not None:
                return newer
        return json.loads(self.evidence[uid]) if uid in self.evidence else None

    def active(self, tid, marker):
        events = self.events.get(tid, ())
        hold = reduce_events(events, verify_release=self.verify)
        if hold is None or hold.unresolved or marker not in hold.episodes:
            return None
        return next((e for e in events if e['kind'] == 'contained' and e['uid'] == marker), None)


def producer_runs(conn, stops):
    """Keep the literal SQL comparison (including NULL/storage types), in one query."""
    rows = [[tid, spec['producer']['task_id'], stop.get('created_at') or 0]
            for tid, (stop, spec) in stops.items()
            if isinstance(spec.get('producer'), dict) and spec['producer'].get('task_id')]
    if not rows:
        return {}
    try:
        return dict(conn.execute(
            "SELECT json_extract(value,'$[0]'),EXISTS(SELECT 1 FROM task_runs "
            "WHERE task_id=json_extract(value,'$[1]') AND outcome='completed' "
            "AND ended_at>json_extract(value,'$[2]')) FROM json_each(?)", (json.dumps(rows),)))
    except sqlite3.Error:  # the literal producer reader treats unavailable history as unknown
        return {}


def read(conn, task_ids, *, replica: Any = ...):
    from hermes_cli import kanban_contained_wait
    owns = not conn.in_transaction
    if owns:
        conn.execute('BEGIN')
    try:
        return kanban_contained_wait.read(conn, task_ids, replica=replica, display=True)
    finally:
        if owns:
            conn.rollback()
