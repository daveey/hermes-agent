"""Exact transaction-bound owner reassessment sender composition."""
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_owner_reassessment as r
from hermes_cli import kanban_owner_reassessment_transport as transport
from hermes_cli import kanban_sync_wire as wire
from tests.hermes_cli import test_kanban_owner_reassessment_lifecycle as lifecycle

worker = lifecycle.worker


def test_actual_park_capture_carries_receipt_only_on_exact_status_op(worker, monkeypatch):
    def body(cap, ctx, agent, evidence):
        with kbc.connect_closing() as conn:
            kb.schedule_task(conn, ctx['task_id'], reason=lifecycle._until(), expected_run_id=ctx['run_id'],
                             awaiting=lifecycle.PRODUCER, owner_reassessment=lifecycle._opt(evidence, int(time.time()) + 120),
                             owner_origin=cap)
            receipt = json.loads(r.pending(conn)[0]['receipt'])
            rows = [dict(row) for row in conn.execute('SELECT * FROM kanban_sync_outbox')]
            carrying = [row for row in rows if 'owner_reassessment_v1' in json.loads(row['fields'])]
            assert len(carrying) == 1, [(x['table_name'], x.get('status_origin'), x['fields']) for x in rows if x['table_name'] == 'tasks']
            row = carrying[0]
            assert row['table_name'] == 'tasks'
            assert row['status_origin'] == receipt['park_event']['uid']
            op = wire.translate(row, '1795610000000-0001-here')
            assert op['fields']['owner_reassessment_v1'] == receipt
            assert op['fields']['status'] == 'scheduled'
            assert op['fields']['owner_reassessment_fallback_status_origin'] == ''
            # The final origin binder must preserve both identities on this op.
            assert row['status_origin'] != op['fields']['owner_reassessment_fallback_status_origin']
            # An unrelated new same-task metadata capture does not acquire a receipt.
            with kb.write_txn(conn):
                conn.execute('UPDATE tasks SET title=? WHERE id=?', ('later', ctx['task_id']))
            later = conn.execute('SELECT fields FROM kanban_sync_outbox ORDER BY rowid DESC LIMIT 1').fetchone()[0]
            assert 'owner_reassessment_v1' not in json.loads(later)
    lifecycle._turn(worker, monkeypatch, body)


@pytest.mark.parametrize('fault', ['frozen', 'origin', 'status', 'other-op'])
def test_exact_capture_refuses_wrong_or_frozen_op(fault):
    with sqlite3.connect(':memory:') as conn:
        r.ensure_schema(conn)
        receipt = {'park_event': {'uid': 'park'}}
        conn.execute(f'INSERT INTO {r.PENDING_TABLE} VALUES (?,?,?,?,?)', ('park', 'task', 'episode', json.dumps(receipt), 1))
        conn.execute('CREATE TABLE kanban_sync_outbox(op_id,row_uid,table_name,op,fields,wire_json,status_origin)')
        conn.execute('INSERT INTO kanban_sync_outbox VALUES (?,?,?,?,?,?,?)',
                     ('op', 'task', 'tasks', 'upsert', json.dumps({'status': 'ready' if fault == 'status' else 'scheduled'}),
                      '{}' if fault == 'frozen' else None, 'other' if fault == 'origin' else 'park'))
        with pytest.raises(r.ReassessmentRefused):
            transport.bind(conn, op_id='missing' if fault == 'other-op' else 'op', task_id='task', event_uid='park')
        assert 'owner_reassessment_v1' not in conn.execute('SELECT fields FROM kanban_sync_outbox').fetchone()[0]
