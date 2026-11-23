"""Bookkeeping consumers preserve semantic bytes and transaction rollback."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal, kanban_sync_client as client
from hermes_cli import kanban_actionability_transport as act, kanban_retain_transport as retain
from tests.hermes_cli import test_kanban_replay_controls as support
from tests.hermes_cli import test_kanban_actionability_transport as diagnostic

live = support.live


def outbox(conn, op):
    return dict(conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (op,)).fetchone())


@pytest.mark.parametrize('kind', ['retain', act.KIND])
def test_unknown_delivery_changes_only_bookkeeping_and_rolls_back(live, kind):
    _, owned, original = support.reserve(live, kind)
    transport = retain if kind == 'retain' else act
    before = support.command(live, kind, owned)
    result = dict(status='deferred', reason='storage_error')
    with pytest.raises(RuntimeError, match='rollback'), kbc.write_txn(live):
        assert transport.record_result(live, original, result) == 'deferred'
        assert outbox(live, owned['op_id'])['attempts'] == original['attempts'] + 1
        raise RuntimeError('rollback')
    assert outbox(live, owned['op_id']) == original
    assert support.command(live, kind, owned) == before
    with kbc.write_txn(live):
        assert transport.record_result(live, original, result) == 'deferred'
    after = outbox(live, owned['op_id'])
    assert after['attempts'] == original['attempts'] + 1 and after['last_error']
    assert after['state'] == 'pending'
    assert {k: v for k, v in after.items() if k not in ('attempts','last_error','retry_after')} == {
        k: v for k, v in original.items() if k not in ('attempts','last_error','retry_after')}
    assert support.command(live, kind, owned)['delivery_state'] == 'unknown'
    assert journal.pending_wire(live) is None


def test_eligibility_hold_does_not_rewrite_command_or_count_a_send(live):
    sync.set_mode(live, 'primary')
    tid, payload = diagnostic.contract(live)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET created_on='macbook' WHERE id=?", (tid,))
    payload['deadline_at'] = 100
    owned = diagnostic.reserve(live, tid, payload, 'escalation')
    original = outbox(live, owned['op_id'])
    other = kb.create_task(live, title='unrelated progress', assignee='worker')
    receiver = support.Controls()
    client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    after = outbox(live, owned['op_id'])
    assert after['last_error'].startswith('diagnostic prerequisite pending; preserve frozen request:')
    assert {k: v for k, v in after.items() if k != 'last_error'} == {
        k: v for k, v in original.items() if k != 'last_error'}
    assert act.command(live, owned['op_id'])['request_json'] == owned['request_json']
    assert receiver.sent[0]['uid'] == other and len(receiver.sent) == 1
    assert json.loads(owned['request_json'])['client_op_id'] == owned['op_id']
