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


@pytest.mark.parametrize('park_kind', ['schedule', 'continue'])
@pytest.mark.parametrize('advertise', [True, False], ids=['certified', 'unsupported'])
@pytest.mark.parametrize('boundary', ['selection', 'push'])
def test_real_park_transaction_orders_certificate_before_its_own_evidence(
        worker, monkeypatch, park_kind, advertise, boundary):
    """Sender integration, not a Board acceptance fake.

    Board prepareOwnerReassessment rejects a preexisting park uid; the task op
    must insert/seal it atomically before its generic event or run-end arrives.
    Unsupported Boards must retain ordinary evidence-first serialization.
    """
    from copy import deepcopy
    from types import SimpleNamespace

    from hermes_cli import kanban_continuation as continuation
    from hermes_cli import kanban_sync_client as client
    from hermes_cli import kanban_sync_parent_order as ordering
    from tests.hermes_cli.test_kanban_owner_reassessment_board_compat import BASE_CAP, NEW

    def body(cap, ctx, agent, evidence):
        with kbc.connect_closing() as conn:
            # Model an acknowledged running card, then retain only actual
            # lifecycle captures. No hand-built outbox or evidence-order hints.
            with kb.write_txn(conn):
                conn.execute('UPDATE tasks SET sync_version=5 WHERE id=?', (ctx['task_id'],))
                conn.execute('DELETE FROM kanban_sync_outbox')
            kwargs = dict(expected_run_id=ctx['run_id'], awaiting=lifecycle.PRODUCER,
                          owner_reassessment=lifecycle._opt(evidence, int(time.time()) + 120), owner_origin=cap)
            if park_kind == 'schedule':
                assert kb.schedule_task(conn, ctx['task_id'], reason=lifecycle._until(), **kwargs)
            else:
                result = continuation.continue_task(
                    conn, ctx['task_id'], checkpoint='waiting on example queue', resume_after_seconds=1800, **kwargs)
                assert result['ok'] and result['status'] == 'scheduled'
            receipt = json.loads(r.pending(conn)[0]['receipt'])
            raw = [dict(row) for row in conn.execute('SELECT * FROM kanban_sync_outbox ORDER BY rowid')]
            status, = [row for row in raw if 'owner_reassessment_v1' in json.loads(row['fields'])]
            park, = [row for row in raw if row['table_name'] == 'task_events'
                     and row['row_uid'] == receipt['park_event']['uid']]
            end, = [row for row in raw if row['table_name'] == 'task_runs'
                    and row['row_uid'] == receipt['issuer']['run_uid']]
            # These are real write_txn.bind results, not manually tagged rows.
            assert status['evidence_uid'] == park['evidence_uid'] == park['row_uid']
            assert json.loads(end['fields'])['status'] in ('scheduled', 'continued')
            assert json.loads(status['fields'])['owner_reassessment_v1'] == receipt
            calls = []

            def capture_only(board, batch):
                calls.extend(deepcopy(batch))
                raise OSError('test transport capture: no Board acceptance simulated')

            hub = SimpleNamespace(ops=capture_only, status_origin_capability=lambda board: {
                **BASE_CAP, **({'owner_reassessment_v1': 1} if advertise else {})})
            if boundary == 'selection':
                rows, _, stopped = ordering.select_pending(
                    conn, hub, 'default', {}, now=int(time.time()), limit=100, before_head=False)
                assert not stopped
                ids = [row['op_id'] for row in rows]
            else:
                with pytest.raises(OSError, match='test transport capture'):
                    client.push(conn, hub, 'default', 'here', primary=True, max_batches=1)
                ids = [op['client_op_id'] for op in calls]
                sent, = [op for op in calls if op['client_op_id'] == status['op_id']]
                if advertise:
                    assert sent['fields']['owner_reassessment_v1'] == receipt
                    assert sent['status_origin'] == park['row_uid']
                else:
                    assert not set(NEW) & sent['fields'].keys()
                    assert sent['status_origin'] == ''
            positions = {name: ids.index(row['op_id']) for name, row in
                         [('status', status), ('park', park), ('run_end', end)]}
            assert positions['status'] < positions['run_end'], positions
            if advertise:
                assert positions['status'] < positions['park'], positions
            else:
                assert positions['park'] < positions['status'], positions

    lifecycle._turn(worker, monkeypatch, body)


def _park_only(conn, ctx, cap, evidence):
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET sync_version=5 WHERE id=?', (ctx['task_id'],))
        conn.execute('DELETE FROM kanban_sync_outbox WHERE 1=1')
    assert kb.schedule_task(conn, ctx['task_id'], reason=lifecycle._until(), expected_run_id=ctx['run_id'],
                            awaiting=lifecycle.PRODUCER,
                            owner_reassessment=lifecycle._opt(evidence, int(time.time()) + 120), owner_origin=cap)
    raw = [dict(row) for row in conn.execute('SELECT * FROM kanban_sync_outbox ORDER BY rowid')]
    status, = [row for row in raw if 'owner_reassessment_v1' in json.loads(row['fields'])]
    park, = [row for row in raw if row['table_name'] == 'task_events' and row['row_uid'] == status['evidence_uid']]
    return status, park


@pytest.mark.parametrize('fault', ['exact', 'receipt', 'origin', 'evidence', 'unsupported'])
def test_only_exact_bound_park_is_exempt_from_evidence_first(worker, monkeypatch, fault):
    """Controls: any broken binding or unsupported capability keeps the ordinary event-first order."""
    from hermes_cli import kanban_sync_evidence_order as eo

    def body(cap, ctx, agent, evidence):
        with kbc.connect_closing() as conn:
            status, park = _park_only(conn, ctx, cap, evidence)
            if fault == 'receipt':
                f = json.loads(status['fields'])
                f['owner_reassessment_v1']['issuer'] = {'run_uid': 'other'}
                status['fields'] = json.dumps(f)
            elif fault == 'origin':
                status['status_origin'] = 'other'
            elif fault == 'evidence':
                status['evidence_uid'] = 'other'
            first = [x['op_id'] for x in eo.firsts(conn, status, reassessment=fault != 'unsupported')]
            if fault == 'exact':
                assert transport.bound_park_uid(conn, status) == park['row_uid']
                assert park['op_id'] not in first
            else:
                if fault != 'unsupported':
                    assert transport.bound_park_uid(conn, status) is None
                if fault in ('origin', 'evidence'):
                    return  # firsts keys on the (now altered) evidence/origin column itself
                assert park['op_id'] in first
    lifecycle._turn(worker, monkeypatch, body)


@pytest.mark.parametrize('cap_value', [False, 0, 2, '1', None])
def test_invalid_reassessment_capability_value_is_unsupported(cap_value):
    from hermes_cli import kanban_sync_parent_order as ordering
    assert not ordering.reassessment_supported({})
    assert not ordering.reassessment_supported({'owner_reassessment_v1': cap_value})
    assert ordering.reassessment_supported({'owner_reassessment_v1': 1})


@pytest.mark.parametrize('exclusion', ['backoff', 'allowed', 'before-head', 'frozen-unsupported', 'frozen-fallback'])
def test_pending_certificate_holds_its_exact_park_when_excluded(worker, monkeypatch, exclusion):
    from types import SimpleNamespace
    from hermes_cli import kanban_sync_parent_order as ordering
    from hermes_cli import kanban_sync_backlog as backlog
    from tests.hermes_cli.test_kanban_owner_reassessment_board_compat import BASE_CAP

    def body(cap, ctx, agent, evidence):
        with kbc.connect_closing() as conn:
            status, park = _park_only(conn, ctx, cap, evidence)
            now = int(time.time())
            if exclusion == 'backoff':
                conn.execute('UPDATE kanban_sync_outbox SET retry_after=? WHERE op_id=?',
                             (now + 3600, status['op_id']))
            elif exclusion in ('frozen-unsupported', 'frozen-fallback'):
                frozen = wire.translate(status, '1795610000000-0001-here')
                if exclusion == 'frozen-fallback':
                    from tests.hermes_cli.test_kanban_owner_reassessment_board_compat import NEW
                    frozen['fields'] = {k: v for k, v in frozen['fields'].items() if k not in NEW}
                    frozen['status_origin'] = ''
                conn.execute('UPDATE kanban_sync_outbox SET wire_json=? WHERE op_id=?',
                             (json.dumps(frozen), status['op_id']))
            hub = SimpleNamespace(status_origin_capability=lambda board: {
                **BASE_CAP, **({} if exclusion in ('frozen-unsupported', 'frozen-fallback')
                               else {'owner_reassessment_v1': 1})})
            if exclusion == 'frozen-fallback':
                rows = ordering.parent_first(conn, [park], now, 2, reassessment=False)
                assert park['op_id'] in [x['op_id'] for x in rows]
                return
            if exclusion == 'allowed':
                rows = ordering.parent_first(conn, [park], now, 2, allowed={park['op_id']}, reassessment=True)
            else:
                if exclusion == 'before-head':
                    monkeypatch.setattr(backlog, 'sendable_before_head', lambda c, h, b, rows, seen:
                                        ([x for x in rows if x['op_id'] != status['op_id']], False))
                rows, _, _ = ordering.select_pending(conn, hub, 'default', {}, now=now, limit=100,
                                                     before_head=exclusion == 'before-head')
            assert park['op_id'] not in [x['op_id'] for x in rows]
    lifecycle._turn(worker, monkeypatch, body)


@pytest.mark.parametrize('limit', [1, 2])
def test_split_batches_allow_prior_evidence_but_never_park_before_certificate(worker, monkeypatch, limit):
    from hermes_cli import kanban_sync_parent_order as ordering

    def body(cap, ctx, agent, evidence):
        with kbc.connect_closing() as conn:
            status, park = _park_only(conn, ctx, cap, evidence)
            # A real earlier lifecycle event remains unrelated to the certified park.
            prior = dict(conn.execute("SELECT * FROM kanban_sync_outbox WHERE table_name='task_events' "
                                      "AND row_uid!=? ORDER BY rowid LIMIT 1", (park['row_uid'],)).fetchone())
            raw = [prior, park, status]
            sent = []
            while raw:
                batch = ordering.parent_first(conn, raw, int(time.time()), limit, reassessment=True)
                assert batch
                ids = [x['op_id'] for x in batch]
                sent.extend(ids)
                conn.executemany("UPDATE kanban_sync_outbox SET state='acked' WHERE op_id=?", [(x,) for x in ids])
                raw = [x for x in raw if x['op_id'] not in ids]
            assert sent.index(prior['op_id']) < sent.index(status['op_id']) < sent.index(park['op_id'])
    lifecycle._turn(worker, monkeypatch, body)


def test_selection_reads_capability_once_and_two_row_batch_keeps_status_first(worker, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import kanban_sync_parent_order as ordering
    from tests.hermes_cli.test_kanban_owner_reassessment_board_compat import BASE_CAP

    def body(cap, ctx, agent, evidence):
        with kbc.connect_closing() as conn:
            status, park = _park_only(conn, ctx, cap, evidence)
            reads = []
            hub = SimpleNamespace(status_origin_capability=lambda board: reads.append(1) or {
                **BASE_CAP, 'owner_reassessment_v1': 1})
            rows, _, _ = ordering.select_pending(conn, hub, 'default', {}, now=int(time.time()), limit=2,
                                                 before_head=False)
            ids = [x['op_id'] for x in rows]
            assert len(reads) == 1 and status['op_id'] in ids
            assert park['op_id'] not in ids or ids.index(status['op_id']) < ids.index(park['op_id'])
            ordering.select_pending(conn, hub, 'default', {}, now=int(time.time()), limit=100, before_head=False,
                                    origin_cap={**BASE_CAP, 'owner_reassessment_v1': 1})
            assert len(reads) == 1  # explicit snapshot: no extra read
    lifecycle._turn(worker, monkeypatch, body)
