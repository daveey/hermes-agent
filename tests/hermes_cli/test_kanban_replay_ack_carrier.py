"""Normal successor ACK carrier; fixture Board, not deployed receiver proof."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as sender

live = sender.live


class AckReceiver(sender.Receiver):
    def __init__(self):
        super().__init__()
        self.predecessors = []

    def _call(self, method, path, body=None):
        if path.endswith('/ops'):
            op, = body['ops']
            predecessor = op['replay'].get('ack')
            if self.slot['h'] != '0':
                assert predecessor == dict(sequence=self.slot['h'], op_id=self.slot['last_op_id'],
                                           payload_sha256=self.slot['last_payload_sha256'])
                self.slot['a'] = self.slot['h']
            else:
                assert predecessor is None
            self.predecessors.append(predecessor)
        reply = super()._call(method, path, body)
        if path.endswith('/ops'):
            # Serving Board settleReplay at f6f535ae: slim verdict plus explicit
            # consumption/sequence/digest/prefix, never an implied HTTP-200 ACK.
            verdict = json.loads(self.slot['last_verdict'])
            # Board JSON.stringify uses compact JSON; the local adapter must not
            # assume Python's verdict string formatting is the serving slot's.
            self.slot['last_verdict'] = json.dumps(verdict, separators=(',', ':'))
            verdict.update(consumed=True, sequence=self.slot['h'], h=self.slot['h'],
                           payload_sha256=self.slot['last_payload_sha256'])
            return dict(protocol='processed_prefix1', results=[verdict])
        return reply


def test_successor_carries_ack_without_intermediate_standalone_control(live):
    kb.create_task(live, title='first', assignee='worker')
    kb.create_task(live, title='second', assignee='worker')
    receiver = AckReceiver()
    result = client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=2)
    assert result.applied == 2
    assert len(receiver.sent) == 2
    assert receiver.calls == ['capability', 'inspect', 'session', 'ops', 'ops', 'ack']
    assert receiver.predecessors[0] is None
    first = receiver.sent[0]
    assert receiver.predecessors[1] == dict(sequence='1', op_id=first['client_op_id'],
                                          payload_sha256=first['replay']['payload_sha256'])
    assert journal.snapshot(live)['settled'] == receiver.slot['a'] == '2'
    assert journal.pending_wire(live) is None


def test_idle_stream_still_acknowledges_last_durable_verdict(live):
    kb.create_task(live, title='idle', assignee='worker')
    receiver = AckReceiver()
    client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert receiver.calls[-1] == 'ack'
    assert receiver.slot['a'] == journal.snapshot(live)['settled'] == '1'
    verdict = json.loads(receiver.slot['last_verdict'])
    assert verdict['client_op_id'] == receiver.sent[0]['client_op_id']


@pytest.mark.parametrize('field,value', [
    ('consumed', False), ('consumed', 1), ('consumed', None),
    ('sequence', '2'), ('sequence', 1), ('h', '2'),
    ('payload_sha256', 'f' * 64), ('client_op_id', 'foreign-operation'),
    ('status', 'already_processed'), ('outcome_unknown', True), ('dup', True),
])
def test_unbound_or_nonconsuming_reply_requires_inspect_before_prune(live, field, value):
    tid = kb.create_task(live, title='untrusted final-looking reply', assignee='worker')

    class DamagedReply(AckReceiver):
        def _call(self, method, path, body=None):
            if path.endswith('/inspect') and self.sent:
                raise OSError('independent inspection unavailable')
            reply = super()._call(method, path, body)
            if path.endswith('/ops'):
                reply['results'][0][field] = value
            return reply

    receiver = DamagedReply()
    with pytest.raises(OSError, match='independent inspection unavailable'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert journal.snapshot(live)['settled'] == '0'
    assert journal.pending_wire(live) is not None
    assert live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] is None
    assert receiver.slot['a'] == '0' and 'ack' not in receiver.calls


def test_successor_disconnect_restart_keeps_frozen_predecessor_and_exact_intent(live, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    kb.create_task(live, title='settled predecessor', assignee='worker')
    kb.create_task(live, title='uncertain successor', assignee='worker')

    class Disconnect(AckReceiver):
        fail = True

        def _call(self, method, path, body=None):
            if path.endswith('/ops') and len(self.sent) == 1 and self.fail:
                raise OSError('successor disconnected before commit')
            return super()._call(method, path, body)

    receiver = Disconnect()
    with pytest.raises(OSError, match='successor disconnected'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=2)
    assert receiver.slot['h'] == '1' and receiver.slot['a'] == '0'
    saved = journal.pending_wire(live)
    assert saved is not None
    assert json.loads(saved)['replay']['ack']['op_id'] == receiver.sent[0]['client_op_id']
    state = journal.snapshot(live)
    assert state['settled'] == '1' and state['pending']['sequence'] == '2'
    path = live.execute('PRAGMA database_list').fetchone()[2]
    live.close()
    receiver.fail = False
    conn = kbc.connect(Path(path))
    try:
        assert journal.pending_wire(conn) == saved
        sender.retry_due(conn, monkeypatch)
        result = client.push(conn, receiver, 'default', 'macbook', primary=False, max_batches=1)
        assert result.applied == 1
        actual = dict(receiver.sent[-1])
        actual['replay'] = dict(actual['replay'])
        actual['replay'].pop('session')
        assert actual == json.loads(saved)
        assert journal.snapshot(conn)['settled'] == receiver.slot['a'] == '2'
    finally:
        conn.close()


@pytest.mark.parametrize('changed', [dict(status='merged'), dict(version=True), dict(version=8)])
def test_ack_readback_never_accepts_changed_verdict_content(live, changed):
    kb.create_task(live, title='ACK verdict content', assignee='worker')

    class ChangedAck(AckReceiver):
        def _call(self, method, path, body=None):
            reply = super()._call(method, path, body)
            if path.endswith('/ack'):
                verdict = json.loads(reply['slot']['last_verdict'])
                verdict.update(changed)
                reply['slot']['last_verdict'] = json.dumps(verdict)
            return reply

    with pytest.raises(ValueError, match='^ack readback mismatch$'):
        client.push(live, ChangedAck(), 'default', 'macbook', primary=False, max_batches=1)
    assert journal.snapshot(live)['settled'] == '1'
    assert journal.pending_wire(live) is None


def test_current_row_reply_enrichment_is_not_retained_history(live):
    kb.create_task(live, title='not the reply row', assignee='worker')

    class Enriched(AckReceiver):
        def _call(self, method, path, body=None):
            reply = super()._call(method, path, body)
            if path.endswith('/ops'):
                reply['results'][0].update(row=dict(title='untrusted current image'),
                                           source_predecessor=dict(title='not historical'))
            return reply

    receiver = Enriched()
    assert client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1).applied == 1
    verdict = json.loads(journal.snapshot(live)['last_verdict'])
    assert 'row' not in verdict and 'source_predecessor' not in verdict


def test_forty_settlements_use_one_ops_roundtrip_each_after_session(live):
    for n in range(40):
        kb.create_task(live, title=f'backlog {n}', assignee='worker')
    receiver = AckReceiver()
    result = client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=40)
    assert result.applied == result.sent == 40
    assert receiver.calls == ['capability', 'inspect', 'session'] + ['ops'] * 40 + ['ack']
    assert receiver.slot['h'] == receiver.slot['a'] == journal.snapshot(live)['settled'] == '40'
    assert journal.pending_wire(live) is None
    assert [op['replay']['sequence'] for op in receiver.sent] == [str(n) for n in range(1, 41)]
