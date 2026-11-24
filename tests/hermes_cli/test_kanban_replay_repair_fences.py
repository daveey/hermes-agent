"""Replay repair fences: no uncertain remap, bounded retries, transactional failure and W=1 ACK."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli import test_kanban_replay_primary as primary
from tests.hermes_cli import test_kanban_replay_repair as repair

live = helpers.live


@pytest.mark.parametrize('mode', [False, True])
@pytest.mark.parametrize('reason,attempts,status', [
    ('invalid', 0, 'rejected'), ('storage_error', 19, 'rejected'), ('storage_error', 0, 'cancelled'),
])
def test_non_retryable_or_exhausted_verdict_keeps_visible_identity(live, mode, reason, attempts, status):
    tid = kb.create_task(live, title='bounded refusal', assignee='worker')
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_outbox SET attempts=?', (attempts,))
    receiver = primary.Primary(live)
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(live, receiver, 'default', 'macbook', primary=mode, max_batches=1)
    original = receiver.sent[0]['client_op_id']
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict.update(status=status, reason=reason, detail='bounded fault')
    receiver.slot['last_verdict'] = json.dumps(verdict)
    receiver.lose_reply = False
    receiver.evidence = dict(board='default', table='tasks', uid=tid, state='live', head=99,
                             row=dict(uid=tid, title='bounded refusal', status='todo', version=19))
    client.push(live, receiver, 'default', 'macbook', primary=mode, max_batches=1)
    source = repair.row(live, original)
    assert source['state'] == 'rejected' and source['attempts'] == attempts + 1
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid=? AND state='pending'", (tid,)).fetchone()
    assert receiver.slot['a'] == '1' and len(receiver.sent) == 1


def test_remap_rollback_retains_exact_frozen_source_and_final_slot(live, monkeypatch):
    from hermes_cli import kanban_sync_omissions as omissions
    kb.create_task(live, title='atomic remap', assignee='worker')
    receiver = helpers.Receiver()
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    original = receiver.sent[0]['client_op_id']
    before, frozen, state = repair.row(live, original), journal.pending_wire(live), journal.snapshot(live)
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict.update(status='rejected', reason='storage_error')
    receiver.slot['last_verdict'] = json.dumps(verdict)
    receiver.lose_reply = False
    def fail(*args):
        raise OSError('remap persistence fault')
    with monkeypatch.context() as m:
        m.setattr(omissions, 'remap', fail)
        with pytest.raises(OSError, match='remap persistence fault'):
            client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert repair.row(live, original) == before
    assert journal.snapshot(live) == state and journal.pending_wire(live) == frozen
    assert receiver.slot['a'] == '0' and len(receiver.sent) == 1
    client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert repair.row(live, original) is None and receiver.slot['a'] == '1'


class Window(helpers.Receiver):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.ack_failure = False

    def _call(self, method, path, body=None):
        if path.endswith('/ops') and self.slot['h'] != '0':
            assert not self.conn.in_transaction, 'predecessor settlement must already be committed'
            assert journal.snapshot(self.conn)['settled'] == self.slot['h']
            op, = body['ops']
            assert op['replay']['ack'] == dict(sequence=self.slot['h'], op_id=self.slot['last_op_id'],
                                              payload_sha256=self.slot['last_payload_sha256'])
            if self.slot['a'] != self.slot['h'] and self.ack_failure:
                raise OSError('ACK not committed')
        if path.endswith('/ack') and self.ack_failure:
            raise OSError('ACK not committed')
        return super()._call(method, path, body)


def test_multibatch_unrelated_intents_ack_each_slot_before_next_send(live):
    tids = {kb.create_task(live, title=f'work {i}', assignee='worker') for i in range(3)}
    receiver = Window(live)
    client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=25)
    assert {op['uid'] for op in receiver.sent if op['table'] == 'tasks'} == tids
    assert receiver.slot['a'] == receiver.slot['h'] and journal.pending_wire(live) is None


def test_ack_failure_holds_issued_successor_and_preserves_other_intents(live, monkeypatch):
    for i in range(2):
        kb.create_task(live, title=f'ACK gated {i}', assignee='worker')
    receiver = Window(live)
    receiver.ack_failure = True
    with pytest.raises(OSError, match='ACK not committed'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=25)
    assert len(receiver.sent) == 1 and journal.snapshot(live)['settled'] == '1'
    frozen = journal.pending_wire(live)
    assert frozen is not None and receiver.slot['a'] == '0'
    assert journal.snapshot(live)['pending']['sequence'] == '2'
    assert json.loads(frozen)['replay']['ack']['op_id'] == receiver.sent[0]['client_op_id']
    assert live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state='pending'").fetchone()[0] > 0
    receiver.ack_failure = False
    helpers.retry_due(live, monkeypatch)
    # Recovery acknowledges the durable predecessor, then retries the SAME
    # issued successor before selecting any additional identity.
    client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=25)
    assert len(receiver.sent) > 1 and receiver.slot['a'] == receiver.slot['h']
