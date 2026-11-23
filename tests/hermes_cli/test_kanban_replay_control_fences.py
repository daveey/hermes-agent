"""Control custody, prerequisite and independent confirmation boundaries."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal, kanban_replay_controls as controls
from hermes_cli import kanban_sync_client as client, kanban_sync_envelope as envelopes
from hermes_cli import kanban_actionability_transport as act, kanban_retain_transport as retain
from tests.hermes_cli import test_kanban_replay_controls as support
from tests.hermes_cli import test_kanban_retain_receipt as receipts

live = support.live


@pytest.mark.parametrize('kind', ['retain', act.KIND])
def test_command_and_journal_settlement_roll_back_together(live, monkeypatch, kind):
    _, owned, _ = support.reserve(live, kind)
    receiver = support.Controls()
    receiver.lose_reply = True
    with pytest.raises(OSError):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    frozen = journal.pending_wire(live)
    settle = controls.settle
    def fail(conn, row, verdict):
        settle(conn, row, verdict)
        raise RuntimeError('after command settlement')
    monkeypatch.setattr(controls, 'settle', fail)
    receiver.lose_reply = False
    with pytest.raises(RuntimeError, match='after command'):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert support.command(live, kind, owned)['delivery_state'] == 'pending'
    assert live.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],)).fetchone()
    assert journal.pending_wire(live) == frozen and receiver.slot['a'] == '0'
    monkeypatch.setattr(controls, 'settle', settle)
    client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert support.command(live, kind, owned)['delivery_state'] == 'acknowledged'
    assert len(receiver.sent) == 1


@pytest.mark.parametrize('kind', support.KINDS)
def test_pending_control_is_not_synthetic_parent_and_guards_import(live, kind):
    tid, _, _ = support.reserve(live, kind)
    receiver = support.Controls()
    receiver.lose_reply = True
    with pytest.raises(OSError):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    frozen = journal.pending_wire(live)
    with kbc.write_txn(live):
        envelopes.prune_parents(live)
        with pytest.raises(ValueError, match='import overlaps replay custody'):
            journal.guard_import(live, ('tasks',), {'tasks': {tid}})
        assert journal.guard_import(live, ('tasks',), {'tasks': {'unrelated'}})
    assert journal.pending_wire(live) == frozen


@pytest.mark.parametrize('kind', ['retain', act.KIND])
@pytest.mark.parametrize('status', ['merged', 'deferred', 'cancelled'])
def test_nonordinary_command_verdict_does_not_invent_row_success(live, kind, status):
    _, owned, _ = support.reserve(live, kind)
    receiver = support.Controls()
    receiver.lose_reply = True
    with pytest.raises(OSError):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    frozen = journal.pending_wire(live)
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict['status'] = status
    receiver.slot['last_verdict'] = json.dumps(verdict)
    receiver.lose_reply = False
    if status == 'cancelled':
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
        assert support.command(live, kind, owned)['delivery_state'] == 'rejected'
        assert receiver.slot['a'] == '1'
    else:
        with pytest.raises(ValueError, match='unsupported final command disposition'):
            client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
        assert journal.pending_wire(live) == frozen and receiver.slot['a'] == '0'
        assert support.command(live, kind, owned)['delivery_state'] == 'pending'


def test_release_cancelled_and_invalid_deferred_retain_visible_custody(live):
    _, owned, _ = support.reserve(live, 'release')
    receiver = support.Controls()
    receiver.lose_reply = True
    with pytest.raises(OSError):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    frozen = journal.pending_wire(live)
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict['status'] = 'deferred'
    receiver.slot['last_verdict'] = json.dumps(verdict)
    receiver.lose_reply = False
    with pytest.raises(ValueError, match='unsupported final command disposition'):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert journal.pending_wire(live) == frozen and receiver.slot['a'] == '0'
    verdict['status'] = 'cancelled'
    receiver.slot['last_verdict'] = json.dumps(verdict)
    result = client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert result.cancelled == 1 and receiver.slot['a'] == '1'
    row = live.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],)).fetchone()
    assert row['state'] == 'rejected' and row['last_error'].startswith('cancelled:')
    assert journal.pending_wire(live) is None and len(receiver.sent) == 1


def test_retain_capability_refusal_precedes_allocation_and_retry_is_rechecked(live):
    support.reserve(live, 'retain')
    receiver = support.Controls()
    good = receiver.decision_retain_capability
    receiver.decision_retain_capability = lambda board: {}
    with pytest.raises(ValueError, match='unsupported'):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert journal.pending_wire(live) is None and not receiver.sent
    receiver.decision_retain_capability = good
    original_call = receiver._call
    def uncommitted(method, path, body=None):
        if path.endswith('/ops'):
            raise OSError('lost before commit')
        return original_call(method, path, body)
    receiver._call = uncommitted
    with pytest.raises(OSError, match='before commit'):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    frozen = journal.pending_wire(live)
    receiver._call = original_call
    receiver.decision_retain_capability = lambda board: {}
    with pytest.raises(ValueError, match='unsupported'):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert journal.pending_wire(live) == frozen and not receiver.sent


@pytest.mark.parametrize('kind', ['retain', act.KIND])
@pytest.mark.parametrize('settled', [False, True])
def test_confirmation_bound_transport_never_uses_legacy_and_still_needs_evidence(live, kind, settled):
    if kind == 'retain':
        sync.set_mode(live, 'primary')
        owned, _, _, hub = receipts.fixture(live)
        key = owned['run_uid']
        confirm = retain.confirm
    else:
        _, owned, _ = support.reserve(live, kind)
        hub = support.Controls()
        hub.ident = SimpleNamespace(gateway='macbook')
        hub.row_evidence = lambda *args: (_ for _ in ()).throw(ValueError('canonical receipt missing'))
        key = owned['op_id']
        confirm = act.confirm
    hub.ops = lambda *args: pytest.fail('strict confirmation re-entered legacy /ops')
    if settled:
        with kbc.write_txn(live):
            table = 'kanban_failure_commands' if kind == 'retain' else 'kanban_actionability_commands'
            live.execute(f"UPDATE {table} SET delivery_state='acknowledged' WHERE op_id=?", (owned['op_id'],))
            live.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],))
        if kind == 'retain':
            result = confirm(live, hub, 'default', key)
            assert result['terminal_for_run'] and not result['dependency_satisfied']
            assert retain.command(live, key)['delivery_state'] == 'confirmed'
        else:
            with pytest.raises(ValueError, match='canonical receipt missing'):
                confirm(live, hub, 'default', key)
            assert act.command(live, key)['delivery_state'] == 'acknowledged'
    else:
        with pytest.raises(ValueError, match='still in replay custody'):
            confirm(live, hub, 'default', key)


def test_due_escalation_loss_reopen_then_independent_confirmation(live):
    from tests.hermes_cli import test_kanban_actionability_transport as diagnostic
    sync.set_mode(live, 'primary')
    tid, payload = diagnostic.contract(live)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET created_on='macbook' WHERE id=?", (tid,))
    owned = diagnostic.reserve(live, tid, payload, 'escalation')
    wire = json.loads(owned['request_json'])
    root_wire = act.envelope(tid, wire['payload'], 'contract')
    root = dict(contract=wire['payload'], home_gateway='macbook', request_uid=root_wire['client_op_id'],
                request_sha256=act.digest(act.canonical(root_wire)))
    envelope = dict(root_task=tid, root_episode=None, contract_key=wire['payload']['contract_key'],
                    contract_event_uid=wire['contract_uid'], notice_event_uid=wire['notice_uid'],
                    deadline_at=wire['payload']['deadline_at'], kind='administrative_only')
    receipt = dict(request_sha256=owned['request_sha256'], request_uid=owned['op_id'], contract=wire['payload'],
                   contract_event_uid=wire['contract_uid'], notice_event_uid=wire['notice_uid'], home_gateway='macbook',
                   outcome='route_unavailable', destination=dict(task_id=tid, kind='unavailable'), envelope=envelope)
    payloads = {wire['contract_uid']: ('actionability_contract', root),
                wire['receipt_uid']: ('actionability_escalation_recorded', receipt),
                wire['notice_uid']: ('decision_requested', dict(actionability=envelope))}
    receiver = support.Controls()
    receiver.ident = SimpleNamespace(gateway='macbook')
    receiver.actionability_capability = lambda board: dict(actionability_record=1, frozen_replay=1,
        diagnostic_only=True, hlc='0000000010000-0000-server')
    def evidence(board, table, uid):
        assert not live.in_transaction
        kind, data = payloads[uid]
        return dict(board=board, table=table, uid=uid, state='live', row=dict(uid=uid, task_id=tid,
                    kind=kind, version=1, created_at=1, origin_gateway='macbook', payload=json.dumps(data)))
    receiver.row_evidence = evidence
    receiver.lose_reply = True
    with pytest.raises(OSError):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    reopened = support.sender.reopen(live)
    live = reopened
    receiver.lose_reply = False
    try:
        client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1)
        assert len(receiver.sent) == 1 and receiver.slot['a'] == '1'
        assert act.command(reopened, owned['op_id'])['delivery_state'] == 'acknowledged'
        result = act.confirm(reopened, receiver, 'default', owned['op_id'])
        assert result['delivery_state'] == 'confirmed' and result['outcome'] == 'route_unavailable'
        assert result['terminal_for_run'] is False and result['execution_authority'] is False
    finally:
        reopened.close()


def test_early_escalation_keeps_no_slot_and_unrelated_intent_progresses(live):
    from tests.hermes_cli import test_kanban_actionability_transport as diagnostic
    sync.set_mode(live, 'primary')
    tid, payload = diagnostic.contract(live)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET created_on='macbook' WHERE id=?", (tid,))
    payload['deadline_at'] = 100
    owned = diagnostic.reserve(live, tid, payload, 'escalation')
    other = kb.create_task(live, title='unrelated progress', assignee='worker')
    receiver = support.Controls()
    client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert receiver.sent[0]['uid'] == other
    assert journal.snapshot(live)['settled'] == '1'
    assert act.command(live, owned['op_id'])['delivery_state'] == 'pending'
    assert live.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],)).fetchone()
