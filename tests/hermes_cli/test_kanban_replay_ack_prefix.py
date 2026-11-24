"""Recover an exact retained successor without regressing an authenticated ACK."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_replay_reconcile import reconcile
from tests.hermes_cli.test_kanban_replay_sender import Receiver, live

__all__ = ['live']


@pytest.mark.parametrize('remote_ack', ['1', '2'])
def test_exact_successor_recovers_after_predecessor(live, remote_ack):
    receiver = Receiver()
    kb.create_task(live, title='predecessor', assignee='worker')
    client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    kb.create_task(live, title='successor', assignee='worker')
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    receiver.lose_reply = False
    # Another copy may have ACKed this exact successor while we retained it.
    receiver.slot['a'] = remote_ack
    assert journal.snapshot(live)['settled'] == '1'
    assert reconcile(journal.snapshot(live), receiver.slot)['action'] == 'record'
    before = len(receiver.calls)
    result = client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert result.applied == 1 and result.sent == 0
    assert len(receiver.sent) == 2
    assert receiver.calls[before:].count('ack') == (1 if remote_ack == '1' else 0)
    assert journal.snapshot(live)['settled'] == receiver.slot['a'] == '2'
    assert journal.pending_wire(live) is None
    assert not live.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?',
                            (receiver.sent[-1]['client_op_id'],)).fetchone()


@pytest.mark.parametrize('remote_ack', ['0', '1'])
def test_idle_settled_ack_only_advances_behind_prefix(live, remote_ack):
    receiver = Receiver()
    kb.create_task(live, title='settled', assignee='worker')
    client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    receiver.slot['a'] = remote_ack
    before = len(receiver.calls)
    result = client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=0)
    assert result.sent == result.applied == 0
    assert len(receiver.sent) == 1
    assert receiver.calls[before:].count('ack') == (1 if remote_ack == '0' else 0)
    assert receiver.slot['a'] == journal.snapshot(live)['settled'] == '1'
