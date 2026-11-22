"""Diagnostic commands use real SQLite lifecycle/outbox; ACK never means receipt."""
import hashlib
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_failure_transport as support

live = support.live


def contract(conn):
    tid = kb.create_task(conn, title='diagnostic original scope', assignee='default')
    with kbc.write_txn(conn):
        # Canonical-home evidence normally arrives on the task's first Board ACK.
        conn.execute("UPDATE tasks SET created_on='fixture',sync_version=1 WHERE id=?", (tid,))
        conn.execute('DELETE FROM kanban_sync_outbox')
    return tid, {'version': 1, 'facts': {'status': 'ready', 'assignee': 'default', 'current_run_uid': None,
        'root_episode': None, 'source_uids': [], 'dependencies': []}, 'owner': {'kind': 'unavailable', 'refs': []},
        'blocker': {'prerequisite': 'administrative owner', 'trigger': 'explicit owner decision', 'evidence': []},
        'action': 'repair_lifecycle_contract', 'instruction': 'Repair diagnostic custody, not original scope',
        'first_observed': 1, 'deadline_at': 2, 'legacy': False}


def reserve(conn, tid, payload, phase='contract'):
    from hermes_cli import kanban_actionability_transport as act
    with kbc.write_txn(conn):
        return act.reserve(conn, tid, payload, phase=phase)


def test_wire_custody_and_outbox_atomic_rollback_and_no_original_effect(live):
    from hermes_cli import kanban_actionability_transport as act
    tid, payload = contract(live)
    before = dict(live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    with pytest.raises(RuntimeError), kbc.write_txn(live):
        act.reserve(live, tid, payload)
        raise RuntimeError('rollback diagnostic command')
    assert not sync.outbox(live)
    owned = reserve(live, tid, payload)
    assert owned['request_sha256'] == hashlib.sha256(owned['request_json'].encode()).hexdigest()
    assert dict(live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == before
    assert len(sync.outbox(live)) == 1
    assert reserve(live, tid, payload) == owned
    assert act.outbound(live, sync.outbox(live)[0]) == json.loads(owned['request_json'])
    with pytest.raises(ValueError, match='changed'):
        reserve(live, tid, {**payload, 'deadline_at': 3})


@pytest.mark.parametrize('answer', ['lost', 'ack', 'storage_error', 'missing', 'malformed'])
def test_real_push_retains_original_identity_after_unknown_or_ack(live, answer):
    from hermes_cli import kanban_actionability_transport as act
    tid, payload = contract(live)
    owned = reserve(live, tid, payload)
    wire = json.loads(owned['request_json'])
    class Hub:
        def actionability_capability(self, board):
            return {'actionability_record': 1, 'frozen_replay': 1, 'diagnostic_only': True,
                    'hlc': '0000000000001-0000-server'}
        def ops(self, board, sent):
            assert sent == [wire]
            if answer == 'lost':
                raise OSError('lost reply')
            if answer == 'missing':
                return {'results': []}
            if answer == 'malformed':
                return {'results': [{'client_op_id': owned['op_id'], 'status': 'merged'}]}
            return {'results': [{'client_op_id': owned['op_id'], 'status': 'applied' if answer == 'ack' else 'rejected', 'reason': 'storage_error'}]}
    if answer == 'lost':
        with pytest.raises(OSError):
            client.push(live, Hub(), 'default', 'fixture', primary=True, max_batches=1)
    else:
        client.push(live, Hub(), 'default', 'fixture', primary=True, max_batches=1)
    current = act.command(live, owned['op_id'])
    assert current['request_json'] == owned['request_json'] and current['canonical_receipt_json'] is None
    if answer == 'ack':
        assert current['delivery_state'] == 'acknowledged' and not sync.outbox(live)
    else:
        assert len(sync.outbox(live)) == 1 and sync.outbox(live)[0]['op_id'] == owned['op_id']
    assert live.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'ready'


def test_deleted_disposable_indexes_do_not_renew_contract_deadline(live):
    from hermes_cli import kanban_actionability_transport as act
    tid, payload = contract(live)
    root = reserve(live, tid, payload)
    # Transport custody is NOT a disposable scheduling index, even after its ACK.
    with kbc.write_txn(live):
        act.record_result(live, sync.outbox(live)[0], {'status': 'applied'})
        live.execute('CREATE TABLE kanban_actionability(task_id TEXT)')
        live.execute('DROP TABLE kanban_actionability')
    assert reserve(live, tid, payload) == act.command(live, root['op_id'])
    assert not sync.outbox(live)
    with pytest.raises(ValueError, match='changed'):
        reserve(live, tid, {**payload, 'first_observed': 10, 'deadline_at': 11})


def test_early_escalation_does_not_block_unrelated_unissued_work(live):
    from hermes_cli import kanban_actionability_transport as act
    tid, payload = contract(live)
    payload['deadline_at'] = 100
    pending = reserve(live, tid, payload, 'escalation')
    other = kb.create_task(live, title='unrelated executable sync progress', assignee='default')
    class Hub:
        def actionability_capability(self, board):
            return {'actionability_record': 1, 'frozen_replay': 1, 'diagnostic_only': True,
                    'hlc': '0000000001000-0000-server'}
        def ops(self, board, sent):
            assert all(op['op'] != act.KIND for op in sent), 'early escalation reached outbound/stream issuance'
            return {'results': [{'client_op_id': op['client_op_id'], 'status': 'applied'} for op in sent]}
    result = client.push(live, Hub(), 'default', 'fixture', primary=True)
    assert result.applied > 0
    assert act.command(live, pending['op_id'])['request_json'] == pending['request_json']
    assert [r['op_id'] for r in sync.outbox(live)] == [pending['op_id']]
    assert live.execute('SELECT status FROM tasks WHERE id=?', (other,)).fetchone()[0] == 'ready'


@pytest.mark.parametrize('unsupported', [True, False])
def test_contract_capability_prerequisite_does_not_block_unrelated_work(live, unsupported):
    from hermes_cli import kanban_actionability_transport as act
    tid, payload = contract(live)
    pending = reserve(live, tid, payload)
    second = reserve(live, tid, {**payload, 'blocker': {**payload['blocker'], 'trigger': 'another diagnostic prerequisite'}})
    kb.create_task(live, title='unrelated while diagnostic capability unavailable', assignee='default')
    class Hub:
        probes = 0
        def actionability_capability(self, board):
            self.probes += 1
            if unsupported:
                return {}
            raise OSError('isolated capability transport outage')
        def ops(self, board, sent):
            assert all(op['op'] != act.KIND for op in sent)
            return {'results': [{'client_op_id': op['client_op_id'], 'status': 'applied'} for op in sent]}
    hub = Hub()
    assert client.push(live, hub, 'default', 'fixture', primary=True).applied > 0
    assert hub.probes == 1
    assert {r['op_id'] for r in sync.outbox(live)} == {pending['op_id'], second['op_id']}
    assert act.command(live, pending['op_id'])['request_json'] == pending['request_json']
    assert act.command(live, second['op_id'])['request_json'] == second['request_json']


def test_bounded_eligibility_rotates_across_restart_without_starving_later_diagnostics(live):
    from types import SimpleNamespace
    from hermes_cli import kanban_actionability_transport as act
    tid, payload = contract(live)
    owned = []
    for index in range(33):
        p = {**payload, 'blocker': {**payload['blocker'], 'trigger': f'isolated prerequisite {index}'}}
        owned.append(reserve(live, tid, p, 'escalation'))
    class Hub:
        ident = SimpleNamespace(gateway='fixture')
        probes = []
        def actionability_capability(self, board):
            assert not live.in_transaction
            return {'actionability_record': 1, 'frozen_replay': 1, 'diagnostic_only': True,
                    'hlc': '9999999999999-0000-server'}
        def row_evidence(self, board, table, uid):
            self.probes.append(uid)
            return {'state': 'absent'}
    hub = Hub()
    assert list(act.eligible_pending(live, sync.outbox(live), hub, 'default')) == []
    assert len(hub.probes) == 32
    from pathlib import Path
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    with kbc.connect(path) as reopened:
        assert list(act.eligible_pending(reopened, sync.outbox(reopened), hub, 'default')) == []
    assert len(set(hub.probes)) == 33
    assert len(hub.probes) <= 64
    for row in owned:
        assert act.command(live, row['op_id'])['request_json'] == row['request_json']
    assert all(row['attempts'] == 0 for row in sync.outbox(live))


def test_malformed_diagnostic_isolated_before_unrelated_batch_selection(live):
    from hermes_cli import kanban_actionability_transport as act
    tid, payload = contract(live)
    owned = reserve(live, tid, payload)
    other = kb.create_task(live, title='unrelated valid outbox', assignee='default')
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_outbox SET fields=? WHERE op_id=?', ('{', owned['op_id']))
    class Hub:
        def ops(self, board, sent):
            assert all(op['op'] != act.KIND for op in sent)
            return {'results': [{'client_op_id': op['client_op_id'], 'status': 'applied'} for op in sent]}
    assert client.push(live, Hub(), 'default', 'fixture', primary=True).applied > 0
    assert [r[0] for r in live.execute('SELECT op_id FROM kanban_sync_outbox')] == [owned['op_id']]
    assert act.command(live, owned['op_id'])['request_json'] == owned['request_json']
    assert live.execute('SELECT status FROM tasks WHERE id=?', (other,)).fetchone()[0] == 'ready'


def test_administrative_notice_does_not_promise_comment_release():
    from hermes_cli.kanban_decision_ask import notice_text
    text = notice_text({'why': 'scope authority unavailable', 'actionability': {'kind': 'administrative_only'}})
    assert 'never release or rerun' in text and 'the comment wakes it' not in text
