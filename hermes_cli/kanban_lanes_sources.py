"""Read-only, whole-card source selection for lanes presentation. [daveey]

Only primary boards overlay their local (including picked-up) rows. Pending
changes pin the complete local row/history/edges; otherwise a strictly newer
replica revision wins. A deletion is final on either source, as in sync apply.
Accounting stays on the canonical replica; no runs or event ids are merged.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager, ExitStack
from typing import Any

from hermes_cli.kanban_lanes_flow_evidence import Evidence

_CHILDREN = frozenset(('task_events', 'task_runs'))  # direct classifier/held evidence only


@contextmanager
def read_view(conn, *, local=False):
    """One request's short reader snapshots; close only the replica we opened."""
    from hermes_cli import kanban_lanes_cli as cli

    with ExitStack() as scope:
        replica = cli._fleet(conn, local)
        if replica is not None:
            scope.callback(replica.close)
        for db in (conn, replica):
            if db is not None and not db.in_transaction:
                db.execute('BEGIN')
                scope.callback(db.rollback)
        yield presentation(conn, replica)


def presentation(local, fleet=None):
    """Borrowed readers; non-primary/legacy boards retain their old source."""
    from hermes_cli import kanban_db_sync as sync

    if fleet is None:
        return Evidence(local)
    try:
        primary = sync.board_mode(local) == 'primary'
    except sqlite3.Error:
        primary = False
    return Sources(local, fleet) if primary else Evidence(fleet)


def _metadata(db, ids=None):
    """Read open candidates, or their exact counterparts; never all history."""
    from hermes_cli import kanban_lanes_flow as flow

    if ids is None:
        return {r['id']: r for r in db.execute(
            f'SELECT id,status,sync_version FROM tasks WHERE status IN {flow._OPEN}')}
    rows = {}
    ids = list(ids)
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        marks = ','.join('?' for _ in batch)
        rows.update((r['id'], r) for r in db.execute(
            f'SELECT id,status,sync_version FROM tasks WHERE id IN ({marks})', batch))
    return rows


def _selected_source(task_id, own, remote, pending, local, fleet):
    use_local = own is not None and (task_id in pending or remote is None or
        int(own['sync_version'] or 0) >= int(remote['sync_version'] or 0))
    return local if use_local else fleet


def open_counts(out, pol, evidence, now):
    """Replace only open-card metrics, never canonical run/cost/admission usage."""
    from hermes_cli import kanban_lanes as lanes
    from hermes_cli.kanban_session_wait_rows import _stored_needs_human

    for tally in out.values():
        tally.update(open=0, running=0, ready=0, held=0, blocked_on_you=0, oldest_ready_s=None)
    for row in evidence.rows:
        tally = out[pol.lane_of(row['tenant'])]
        tally['open'] += 1
        if row['status'] in ('running', 'ready'):
            tally[row['status']] += 1
        if row['status'] == 'ready':
            age = int(now) - int(row['created_at'] or now)
            previous = tally['oldest_ready_s']
            tally['oldest_ready_s'] = age if previous is None else max(previous, age)
        if row['status'] in ('blocked', 'triage') and _stored_needs_human(row['human_request'], row['block_kind']):
            tally['blocked_on_you'] += 1
        if row['status'] == 'triage' and row['block_kind'] == lanes.GATE_KIND:
            db = evidence.connection(row['id'])
            tally['held'] += int(db.execute(
                f'SELECT 1 FROM tasks WHERE id=? AND {lanes.HELD}', (row['id'],)).fetchone() is not None)


def _deleted(db):
    return {r[0] for r in db.execute(
        "SELECT row_uid FROM kanban_sync_tombstones WHERE table_name='tasks'")}


def _pending(db):
    pending = set()
    for table, uid, raw in db.execute(
            "SELECT table_name,row_uid,fields FROM kanban_sync_outbox WHERE state != 'rejected'"):
        if table == 'tasks':
            pending.add(uid)  # malformed fields do not obscure the row's known identity
        elif table == 'task_links':
            if not isinstance(uid, str) or '>' not in uid or not all(uid.split('>', 1)):
                continue
            pending.add(uid.split('>', 1)[1])
        elif table in _CHILDREN:
            try:
                fields = json.loads(raw)
            except (ValueError, TypeError):
                fields = None
            tid = fields.get('task_id') if isinstance(fields, dict) else None
            if not isinstance(tid, str) or not tid:
                row = db.execute(f'SELECT task_id FROM {table} WHERE uid=?', (uid,)).fetchone()
                tid = row[0] if row else None
            if not isinstance(tid, str) or not tid:
                # A deleted child may have no surviving identity. It cannot pin
                # an invented card or disable freshness for unrelated cards.
                continue
            pending.add(tid)
    return pending


class Sources(Evidence):
    """Request-local selected open cards, lazy parents and source-owned histories.

    Metadata reads cover open candidates and their counterparts; full terminal
    rows and their source metadata are read only when a parent needs one.
    """

    def __init__(self, local, fleet):
        from hermes_cli import kanban_lanes_flow as flow

        self.conn = fleet
        self.local = local
        self._rows: dict[str, Any] = {}
        self._parents: dict[str, list] = {}
        self._urls: dict[str, Any] = {}
        self._readers = {id(db): Evidence(db, rows=[]) for db in (local, fleet)}
        local_meta, fleet_meta = _metadata(local), _metadata(fleet)
        # A terminal counterpart can suppress an open candidate. Unrelated
        # terminal cards need no source decision until used as a parent.
        candidates = local_meta.keys() | fleet_meta.keys()
        local_meta.update(_metadata(local, candidates - local_meta.keys()))
        fleet_meta.update(_metadata(fleet, candidates - fleet_meta.keys()))
        self._pending = _pending(local)
        self._deleted = _deleted(local) | _deleted(fleet)
        self._source: dict[str, Any] = {}
        selected = {}
        for tid in candidates:
            if tid in self._deleted:
                continue
            own, remote = local_meta.get(tid), fleet_meta.get(tid)
            db = _selected_source(tid, own, remote, self._pending, local, fleet)
            self._source[tid] = db
            selected[tid] = own if db is local else remote
        for db in (local, fleet):
            ids = [tid for tid, row in selected.items()
                   if self._source[tid] is db and row['status'] in flow._OPEN]
            for start in range(0, len(ids), 400):
                batch = ids[start:start + 400]
                marks = ','.join('?' for _ in batch)
                self._rows.update((r['id'], r) for r in db.execute(
                    f'SELECT * FROM tasks WHERE id IN ({marks})', batch))
        self.rows = sorted(self._rows.values(), key=lambda r: (r['created_at'], r['id']))
        # Batch only histories selected from this source, never a losing copy.
        for db in (local, fleet):
            self._readers[id(db)].rows = [row for row in self.rows if self._source[row['id']] is db]

    def connection(self, task_id) -> sqlite3.Connection:
        if task_id not in self._source and task_id not in self._deleted:
            own = _metadata(self.local, [task_id]).get(task_id)
            remote = _metadata(self.conn, [task_id]).get(task_id)
            self._source[task_id] = _selected_source(
                task_id, own, remote, self._pending, self.local, self.conn)
        return self._source.get(task_id, self.conn)

    def identity(self, task_id):
        return self._readers[id(self.connection(task_id))].identity(task_id)

    def row(self, task_id):
        if task_id in self._deleted:
            return None
        if task_id not in self._rows:
            self._rows[task_id] = self._readers[id(self.connection(task_id))].row(task_id)
        return self._rows[task_id]

    def events(self, task_id):
        return self._readers[id(self.connection(task_id))].events(task_id)

    def parents(self, task_id):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_parent_states import mirrored_status

        if task_id not in self._parents:
            out = []
            for parent_id, _fallback in kb.parent_states(self.connection(task_id), task_id):
                parent = self.row(parent_id)
                # The full replica and local snapshot are newer than the local
                # remote-parent mirror; absence on both remains missing.
                status = (mirrored_status(self.connection(parent_id), parent_id, parent['status'])
                          if parent is not None else None)
                if status not in ('done', 'archived'):
                    out.append((parent_id, status if status is not None else 'missing'))
            self._parents[task_id] = out
        return self._parents[task_id]
