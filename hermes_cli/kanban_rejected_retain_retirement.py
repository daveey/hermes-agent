"""Transfer ended-run rejected retain transport into immutable rejection custody.

This is not a retain receipt, replay, or lifecycle disposition. The ordinary
classifier stays conservative; missing command custody cannot use this contract.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli.kanban_retain_transport import canonical, command, frozen_wire
from hermes_cli.kanban_sync_retain_drift import audited_rejection


def _snapshot(conn, op, wire):
    """Pin all local guards again in the writer transaction after external reads."""
    task, run = op['row_uid'], wire['run_uid']
    own = command(conn, run)
    task_row = conn.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
    run_row = conn.execute('SELECT * FROM task_runs WHERE uid=?', (run,)).fetchone()
    guards = []
    for table, uid in (('tasks', task), ('task_runs', run)):
        guards.extend(tuple(r) for r in conn.execute(
            "SELECT * FROM kanban_sync_outbox WHERE table_name=? AND row_uid=? AND state='pending'", (table, uid)))
        guards.extend(tuple(r) for r in conn.execute(
            'SELECT * FROM kanban_sync_tombstones WHERE table_name=? AND row_uid=?', (table, uid)))
    return dict(operation=op, command=own, task=dict(task_row) if task_row else None,
                run=dict(run_row) if run_row else None, guards=guards)


def _proof(hub, board, table, uid):
    value = hub.row_evidence(board, table, uid)
    if (value.get('board', board) != board or value.get('table') != table or value.get('uid') != uid
            or type(value.get('head')) is not int or value.get('state') != 'live'
            or not isinstance(value.get('row'), dict) or value['row'].get('uid') != uid):
        raise ValueError('rejected retain authoritative identity unavailable')
    return value


def retire(conn: sqlite3.Connection, hub, board: str, op: dict) -> dict:
    """Fresh task/run evidence, exact local CAS and lossless first-wins archival.

    No canonical writes or local task/run/fence changes. A successor may run:
    neither its lease nor its status is projected from these evidence reads.
    """
    report = dict(op_id=op['op_id'], table=op['table_name'], row_uid=op['row_uid'],
                  classification='unresolved', action='retained', proof='retain custody not proven')
    if not audited_rejection(conn, conn, op):
        return report
    wire = frozen_wire(op)
    before = _snapshot(conn, op, wire)
    if not before['command'] or before['guards'] or before['command'].get('rejected_release_op_id'):
        return report
    try:
        task = _proof(hub, board, 'tasks', op['row_uid'])
        run = _proof(hub, board, 'task_runs', wire['run_uid'])
    except (ValueError, OSError, AttributeError) as exc:
        report['proof'] = str(exc)
        return report
    if (run['row'].get('task_id') != op['row_uid'] or run['row'].get('ended_at') is None
            or wire['run_uid'] in (task['row'].get('current_run_uid'), task['row'].get('lease_run_uid'))):
        return report
    evidence = dict(task=task, run=run)
    text = canonical(before)
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    with write_txn(conn):
        current = conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (op['op_id'],)).fetchone()
        if (current is None or dict(current) != op or _snapshot(conn, op, wire) != before
                or not audited_rejection(conn, conn, op)):
            report['proof'] = 'local custody changed during evidence read'
            return report
        conn.execute('''CREATE TABLE IF NOT EXISTS kanban_rejected_retain_receipts (
            op_id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
            evidence_json TEXT NOT NULL)''')
        for operation in ('UPDATE', 'DELETE'):
            conn.execute(f'CREATE TRIGGER IF NOT EXISTS rejected_retain_receipt_{operation.lower()} '
                         f'BEFORE {operation} ON kanban_rejected_retain_receipts BEGIN '
                         "SELECT RAISE(ABORT,'rejected_retain_receipt_immutable'); END")
        conn.execute('CREATE TRIGGER IF NOT EXISTS rejected_retain_receipt_insert BEFORE INSERT '
                     'ON kanban_rejected_retain_receipts WHEN EXISTS '
                     '(SELECT 1 FROM kanban_rejected_retain_receipts WHERE op_id=NEW.op_id) BEGIN '
                     "SELECT RAISE(ABORT,'rejected_retain_receipt_immutable'); END")
        existing = conn.execute('SELECT * FROM kanban_rejected_retain_receipts WHERE op_id=?', (op['op_id'],)).fetchone()
        if existing is not None and (existing['snapshot_json'] != text or existing['snapshot_sha256'] != digest):
            report['proof'] = 'conflicting immutable retirement custody'
            return report
        if existing is None:
            conn.execute('INSERT INTO kanban_rejected_retain_receipts VALUES (?,?,?,?)',
                         (op['op_id'], text, digest, canonical(evidence)))
        conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id=? AND state='rejected'", (op['op_id'],))
    report.update(classification='custodied_rejection', action='retired', evidence=evidence,
                  proof='ended-run rejected retain transport archived losslessly; retain was not applied')
    return report
