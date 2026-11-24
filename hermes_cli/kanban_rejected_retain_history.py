"""Archive rejected transport with historical correlation, never lifecycle authority.

Separate from live command custody. Only evicted terminal tasks/runs qualify;
no command reconstruction, task projection, canonical write or original replay.
"""
from __future__ import annotations

import base64
import hashlib
import sqlite3
from pathlib import Path

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_retain_transport import canonical, frozen_wire
from hermes_cli import kanban_rejected_retain_source as source


def _typed(value):
    """Injective JSON encoding preserves SQLite BLOBs without reserved-key collisions."""
    if isinstance(value, bytes):
        return ['bytes', base64.b64encode(value).decode('ascii')]
    if isinstance(value, dict):
        return ['dict', [[k, _typed(v)] for k, v in sorted(value.items())]]
    if isinstance(value, list):
        return ['list', [_typed(v) for v in value]]
    return ['scalar', value]


def _local(conn, wire):
    task, run, event = wire['task'], wire['run_uid'], wire['event_uid']
    rows = []
    for table, column, uid in (('tasks', 'id', task), ('task_runs', 'uid', run),
                               ('task_events', 'uid', event)):
        rows.extend(tuple(r) for r in conn.execute(f'SELECT * FROM {table} WHERE {column}=?', (uid,)))
        rows.extend(tuple(r) for r in conn.execute(
            'SELECT * FROM kanban_sync_outbox WHERE table_name=? AND row_uid=? AND op_id<>?',
            (table, uid, wire['client_op_id'])))
        rows.extend(tuple(r) for r in conn.execute(
            'SELECT * FROM kanban_sync_tombstones WHERE table_name=? AND row_uid=?', (table, uid)))
    # Any surviving current command keeps the existing live-custody contract.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_failure_commands'").fetchone():
        rows.extend(tuple(r) for r in conn.execute(
            'SELECT * FROM kanban_failure_commands WHERE run_uid=? OR task_id=? OR op_id=? OR event_uid=?',
            (run, task, wire['client_op_id'], event)))
    return rows


def _evidence(hub, board, table, uid):
    value = hub.row_evidence(board, table, uid)
    if (not isinstance(value, dict) or value.get('board') != board
            or value.get('table') != table or value.get('uid') != uid
            or type(value.get('head')) is not int or value['head'] < 0
            or type(value.get('floor')) is not int or value['floor'] < 0):
        raise ValueError('historical retain evidence identity unavailable')
    return value


def _command(wire):
    text = canonical(wire)
    return dict(op_id=wire['client_op_id'], request_uid=wire['client_op_id'], task_id=wire['task'],
                run_uid=wire['run_uid'], event_uid=wire['event_uid'], operation_kind='retain',
                delivery_state='rejected', canonical_receipt_json=None, rejected_release_op_id=None,
                request_json=text, request_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest())


def _remote(hub, board, wire):
    task = _evidence(hub, board, 'tasks', wire['task'])
    run = _evidence(hub, board, 'task_runs', wire['run_uid'])
    event = _evidence(hub, board, 'task_events', wire['event_uid'])
    tr, rr = task.get('row'), run.get('row')
    if (task.get('state') != 'live' or task.get('tombstone') is not None
            or not isinstance(tr, dict) or tr.get('uid') != wire['task']
            or not {'current_run_uid', 'lease_run_uid'} <= tr.keys() or tr.get('status') != 'done'
            or tr.get('current_run_uid') is not None or tr.get('lease_run_uid') is not None
            or run.get('state') != 'live' or run.get('tombstone') is not None
            or not isinstance(rr, dict) or rr.get('uid') != wire['run_uid']
            or rr.get('task_id') != wire['task'] or type(rr.get('ended_at')) is not int
            or rr['ended_at'] <= 0 or rr.get('status') != 'blocked'
            or event.get('state') != 'never_existed' or type(event.get('floor')) is not int
            or event['floor'] != 0 or event.get('row') is not None or event.get('tombstone') is not None):
        return None
    return dict(task=task, run=run, event=event)


def _archive(conn, op, text, evidence):
    conn.execute('''CREATE TABLE IF NOT EXISTS kanban_rejected_retain_history_receipts (
        op_id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
        evidence_json TEXT NOT NULL)''')
    for operation in ('UPDATE', 'DELETE'):
        conn.execute(f'CREATE TRIGGER IF NOT EXISTS rejected_retain_history_{operation.lower()} '
                     f'BEFORE {operation} ON kanban_rejected_retain_history_receipts BEGIN '
                     "SELECT RAISE(ABORT,'rejected_retain_history_immutable'); END")
    conn.execute('CREATE TRIGGER IF NOT EXISTS rejected_retain_history_insert BEFORE INSERT '
                 'ON kanban_rejected_retain_history_receipts WHEN EXISTS '
                 '(SELECT 1 FROM kanban_rejected_retain_history_receipts WHERE op_id=NEW.op_id) BEGIN '
                 "SELECT RAISE(ABORT,'rejected_retain_history_immutable'); END")
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    existing = conn.execute('SELECT * FROM kanban_rejected_retain_history_receipts WHERE op_id=?', (op['op_id'],)).fetchone()
    if existing is not None:
        return False  # Never replace or reinterpret an existing historical receipt.
    conn.execute('INSERT INTO kanban_rejected_retain_history_receipts VALUES (?,?,?,?)',
                 (op['op_id'], text, digest, canonical(evidence)))
    conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id=? AND state='rejected'", (op['op_id'],))
    return True


def retire(conn: sqlite3.Connection, hub, board: str, op: dict, state_path: Path) -> dict:
    report: dict = dict(op_id=op['op_id'], table=op['table_name'], row_uid=op['row_uid'],
                  classification='unresolved', action='retained', proof='historical rejected source not proven')
    if (op.get('table_name') != 'tasks' or op.get('op') != 'retain' or op.get('state') != 'rejected'
            or op.get('last_error') != 'missing_decision_episode'):
        return report
    try:
        wire = frozen_wire(op)
        if op['fields'] != canonical(wire) or _local(conn, wire):
            return report
        expected = _command(wire)  # Correlation only; never inserted as command custody.
        before = source.discover(state_path, board, expected, wire)
        if before is None:
            return report
        evidence = _remote(hub, board, wire)
        if evidence is None:
            return report
        snapshot = dict(operation=op, source_encoding='typed-json-v1', source=_typed(before), lifecycle_authority=False)
        text = canonical(snapshot)
        with write_txn(conn):
            current = conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (op['op_id'],)).fetchone()
            if (current is None or dict(current) != op or _local(conn, wire)
                    or source.discover(state_path, board, expected, wire) != before):
                return report
            if not _archive(conn, op, text, evidence):
                return report
        report.update(classification='historical_rejected_source', action='retired', evidence=evidence,
                      proof='rejected transport archived; historical source is not an applied retain receipt')
    except (sqlite3.Error, ValueError, TypeError, KeyError, OSError):
        return report
    return report
