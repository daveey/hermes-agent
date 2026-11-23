"""Replay success keeps worker copies until their replica's next pull. [daveey]"""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_replica_lag as lag
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


@pytest.fixture(autouse=True)
def isolated_lag():
    lag.reset()
    yield
    lag.reset()


def source(conn, child):
    tid = kb.create_task(conn, title='copy with unpublished replica state', assignee='worker')
    if child:
        with kbc.write_txn(conn):
            conn.execute('UPDATE kanban_sync_state SET applying=1')
            conn.execute('UPDATE tasks SET sync_version=3 WHERE id=?', (tid,))
            conn.execute('DELETE FROM kanban_sync_outbox')
            conn.execute('UPDATE kanban_sync_state SET applying=0')
        kb.add_comment(conn, tid, 'author', 'copy must retain this handoff')
    return tid


@pytest.mark.parametrize('child', [False, True])
@pytest.mark.parametrize('status', ['applied', 'merged'])
def test_lost_reply_reopen_success_protects_frozen_task_until_next_pull(live, child, status):
    tid = source(live, child)
    receiver = helpers.Receiver()
    receiver.status = status
    receiver.lose_reply = True
    path = client.shadow_path('default')
    earlier = lag.pull_started(path)
    with pytest.raises(OSError, match='reply lost'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert not lag.lagging(path, [tid])
    frozen = journal.pending_wire(live)
    restarted = helpers.reopen(live)
    try:
        # The routing receipt must come from frozen wire, not a mutable outbox field.
        if child:
            with kbc.write_txn(restarted):
                restarted.execute('UPDATE kanban_sync_outbox SET fields=? WHERE op_id=?',
                                  (json.dumps(dict(task_id='unrelated')), json.loads(frozen)['client_op_id']))
        receiver.lose_reply = False
        result = client.push(restarted, receiver, 'default', 'macbook', primary=False, max_batches=1)
        assert getattr(result, status) == 1 and result.sent == 0 and len(receiver.sent) == 1
        assert journal.pending_wire(restarted) is None and receiver.slot['a'] == '1'
        assert lag.lagging(path, [tid, 'unrelated']) == {tid}, 'successful replay lost replica-lag custody'
        lag.pull_done(path, earlier)
        assert lag.lagging(path, [tid]) == {tid}, 'a pull begun before settlement cannot release it'
        lag.pull_done(path, lag.pull_started(path))
        assert not lag.lagging(path, [tid])
    finally:
        restarted.close()


def test_success_is_published_after_commit_even_when_remote_ack_fails(live, monkeypatch):
    tid = source(live, False)
    receiver = helpers.Receiver()
    actual_acked = lag.acked
    published = []

    def acked(path, task_ids):
        assert not live.in_transaction, 'replica-lag effect escaped local commit'
        assert journal.pending_wire(live) is None and journal.snapshot(live)['settled'] == '1'
        published.append(set(task_ids))
        actual_acked(path, task_ids)

    monkeypatch.setattr(lag, 'acked', acked)
    original_call = receiver._call

    def lost_ack(method, path, body=None):
        if path.endswith('/ack'):
            raise OSError('ack unavailable')
        return original_call(method, path, body)

    receiver._call = lost_ack
    with pytest.raises(OSError, match='ack unavailable'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert published == [{tid}] and lag.lagging(client.shadow_path('default'), [tid]) == {tid}
    receiver._call = original_call
    assert client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=0).sent == 0
    assert published == [{tid}] and len(receiver.sent) == 1 and receiver.slot['a'] == '1'


@pytest.mark.parametrize('status', ['rejected', 'cancelled'])
def test_final_non_success_does_not_publish_replica_success(live, status, monkeypatch):
    tid = source(live, False)
    receiver = helpers.Receiver()
    receiver.status = status
    monkeypatch.setattr(lag, 'acked', lambda *args: pytest.fail('non-success marked as applied'))
    result = client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert getattr(result, status) == 1 and journal.snapshot(live)['settled'] == '1'
    assert not lag.lagging(client.shadow_path('default'), [tid])


def test_synthetic_success_marks_task(live):
    from tests.hermes_cli import test_kanban_replay_current_row as current
    tid, receiver = current.issued_parent(live)
    assert client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1).applied == 1
    assert lag.lagging(client.shadow_path('default'), [tid]) == {tid}


def test_deferred_projection_is_not_replica_success(live, monkeypatch):
    from tests.hermes_cli import test_kanban_replay_synthetic_deferred as synthetic
    tid, receiver = synthetic.issue(live)
    monkeypatch.setattr(lag, 'acked', lambda *args: pytest.fail('deferred custody marked as applied'))
    assert client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1).deferred == 1
    assert journal.pending_wire(live) is None
    assert not lag.lagging(client.shadow_path('default'), [tid])


def test_each_committed_batch_publishes_only_its_own_task(live, monkeypatch):
    first = source(live, False)
    second = source(live, False)
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox WHERE table_name!='tasks'")
    receiver = helpers.Receiver()
    original_call = receiver._call

    def piggyback(method, path, body=None):
        if path.endswith('/ops') and body['ops'][0]['replay'].get('ack'):
            receiver.slot['a'] = body['ops'][0]['replay']['ack']['sequence']
        return original_call(method, path, body)

    receiver._call = piggyback
    published = []

    def acked(path, task_ids):
        assert not live.in_transaction
        published.append(set(task_ids))

    monkeypatch.setattr(lag, 'acked', acked)
    result = client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=3)
    assert result.applied == result.sent == 2
    assert published == [{first}, {second}]
    assert journal.snapshot(live)['settled'] == receiver.slot['a'] == '2'


def test_local_settlement_rollback_cannot_publish_replica_success(live, monkeypatch):
    tid = source(live, False)
    receiver = helpers.Receiver()
    original_recover = journal.recover

    def fail_after_record(*args, **kwargs):
        decision = original_recover(*args, **kwargs)
        if decision['action'] == 'record':
            raise OSError('settlement rollback')
        return decision

    monkeypatch.setattr(journal, 'recover', fail_after_record)
    with pytest.raises(OSError, match='settlement rollback'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert not lag.lagging(client.shadow_path('default'), [tid])
    assert journal.pending_wire(live) is not None and journal.snapshot(live)['settled'] == '0'
    assert receiver.slot['a'] == '0' and len(receiver.sent) == 1
    monkeypatch.setattr(journal, 'recover', original_recover)
    assert client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1).applied == 1
    assert lag.lagging(client.shadow_path('default'), [tid]) == {tid}
    assert len(receiver.sent) == 1
