"""Seeded public-sender/reopen histories against an independent prefix oracle.

The peer is an in-memory contract model, not the deployed Board implementation.
Its unbounded accepted list is test evidence, not proposed production storage.
"""
import copy
import hashlib
import json
import random
from pathlib import Path

import pytest
import rfc8785

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as sender
from tests.hermes_cli.test_kanban_replay_journal import binding

live = sender.live
CUTS = ('capability:before', 'inspect:before', 'session:before', 'session:after',
        'ops:before', 'ops:after', 'ack:before', 'ack:after')


class PrefixOracle:
    """Integer prefix machine independent of sender reconcile/recover helpers."""
    def __init__(self):
        self.h = self.a = 0
        self.session = 1
        self.accepted = []
        self.calls = []
        self.cut = None
        self.fired = []

    def fail(self, action, phase):
        point = action + ':' + phase
        if self.cut == point:
            self.cut = None
            self.fired.append(point)
            raise OSError('model disconnect ' + point)

    def slot(self):
        latest = self.accepted[-1] if self.accepted else None
        return dict(binding(), mode='STRICT', session=str(self.session), h=str(self.h), a=str(self.a),
                    last_op_id=latest['wire']['client_op_id'] if latest else None,
                    last_payload_sha256=latest['wire']['replay']['payload_sha256'] if latest else None,
                    last_verdict=json.dumps(latest['verdict'], separators=(',', ':')) if latest else None)

    def ack(self, evidence):
        latest = self.accepted[-1]
        assert evidence == dict(sequence=str(self.h), op_id=latest['wire']['client_op_id'],
                                payload_sha256=latest['wire']['replay']['payload_sha256'])
        self.a = self.h

    def capability(self, body):
        return dict(processed_prefix=True, board_incarnation=binding()['board_incarnation'],
                    gateway_principal=binding()['gateway_principal'])

    def inspect(self, body):
        return dict(slot=self.slot())

    def open_session(self, body):
        assert body['expected'] == self.slot()
        self.session += 1
        return dict(slot=self.slot())

    def acknowledge(self, body):
        assert body['session'] == str(self.session)
        self.ack(body['ack'])
        return dict(slot=self.slot())

    def apply(self, body):
        op, = body['ops']
        assert body['journal_id'] == binding()['journal_id']
        replay = op['replay']
        assert replay['session'] == str(self.session)
        assert int(replay['sequence']) == self.h + 1, 'accepted prefix was resent or skipped'
        assert replay['generation'] == binding()['generation']
        unsigned = copy.deepcopy(op)
        unsigned['replay'].pop('session')
        digest = unsigned['replay'].pop('payload_sha256')
        assert hashlib.sha256(rfc8785.dumps(unsigned)).hexdigest() == digest
        if self.h:
            self.ack(replay['ack'])
        else:
            assert 'ack' not in replay
        assert self.a == self.h
        assert op['client_op_id'] not in {item['wire']['client_op_id'] for item in self.accepted}
        self.h += 1
        verdict = dict(client_op_id=op['client_op_id'], status='merged' if self.h % 3 == 0 else 'applied',
                       version=self.h + 10)
        self.accepted.append(dict(wire=copy.deepcopy(op), verdict=verdict))
        return dict(protocol='processed_prefix1', results=[dict(verdict, consumed=True,
                    sequence=str(self.h), h=str(self.h), payload_sha256=digest)])

    def _call(self, method, path, body=None):
        action = path.rsplit('/', 1)[-1]
        self.calls.append(action)
        self.fail(action, 'before')
        assert method == ('GET' if action in ('capability', 'inspect') else 'POST')
        handler = dict(capability=self.capability, inspect=self.inspect, session=self.open_session,
                       ack=self.acknowledge, ops=self.apply)[action]
        reply = handler(body)
        self.fail(action, 'after')
        return reply


def custody(conn):
    return (journal.snapshot(conn), journal.pending_wire(conn),
            [tuple(row) for row in conn.execute('SELECT * FROM kanban_sync_outbox ORDER BY op_id')],
            [tuple(row) for row in conn.execute('SELECT * FROM kanban_sync_parent_envelopes ORDER BY task_id')])


def compare_prefix(conn, peer):
    state = journal.snapshot(conn)
    settled = int(state['settled'])
    assert settled <= peer.h <= settled + 1
    assert peer.a <= settled
    pending = state['pending']
    if settled:
        latest = peer.accepted[settled - 1]
        assert (state['last_op_id'], state['last_payload_sha256']) == (
            latest['wire']['client_op_id'], latest['wire']['replay']['payload_sha256'])
    if peer.h > settled:
        assert pending is not None and pending['sequence'] == str(peer.h)
        assert pending['op_id'] == peer.accepted[-1]['wire']['client_op_id']
    if pending:
        assert pending['sequence'] == str(settled + 1)
        retained = json.loads(journal.pending_wire(conn))
        assert retained['client_op_id'] == pending['op_id']
        assert conn.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (pending['op_id'],)).fetchone()
        if peer.h > settled:
            expected = copy.deepcopy(peer.accepted[-1]['wire'])
            expected['replay'].pop('session')
            assert retained == expected
    for item in peer.accepted[:settled]:
        assert not conn.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?',
                                (item['wire']['client_op_id'],)).fetchone(), 'settled source was retained'
    # The transport singleton cannot grow a per-settlement ledger. Business rows
    # and the oracle's accepted list are intentionally excluded from this bound.
    count, state_size = conn.execute('SELECT COUNT(*),MAX(length(state)) FROM kanban_replay_sender1').fetchone()
    assert count == 1 and state_size < 4096


@pytest.mark.parametrize('seed', [17, 101, 733, 2048])
def test_seeded_many_settlements_reopen_and_disconnects(live, seed, record_property):
    rng = random.Random(seed)
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    peer = PrefixOracle()
    conn = live
    turns = 0
    expected_ops = 0
    try:
        # Every cut is reached once before randomized histories, not left to luck.
        schedule = list(CUTS)
        rng.shuffle(schedule)
        schedule += [rng.choice(CUTS + (None, None)) for _ in range(48)]
        for index, cut in enumerate(schedule):
            for n in range(rng.randint(1, 3)):
                kb.create_task(conn, title=f'model {seed}/{index}/{n}', assignee='worker')
            peer.cut = cut
            # An ACK cut needs a successful turn first when the initial send was
            # only an uncertain operation. Keep trying until this one-shot fires.
            for _ in range(4):
                try:
                    client.push(conn, peer, 'default', 'macbook', primary=False, max_batches=rng.randint(1, 5))
                except OSError as exc:
                    assert 'model disconnect' in str(exc)
                turns += 1
                compare_prefix(conn, peer)
                image = custody(conn)
                conn.close()
                conn = kbc.connect(path)
                assert custody(conn) == image, 'reopen changed retained custody'
                if peer.cut is None:
                    break
            assert peer.cut is None, (seed, cut)
        peer.cut = None
        # Drain all intentionally produced business work; no fixture deletion or
        # source rewriting is used to make the journal look empty.
        expected_ops = len(peer.accepted) + conn.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0]
        for _ in range(expected_ops + 2):
            client.push(conn, peer, 'default', 'macbook', primary=False, max_batches=25)
            turns += 1
            compare_prefix(conn, peer)
            if not conn.execute('SELECT 1 FROM kanban_sync_outbox').fetchone():
                break
        assert not conn.execute('SELECT 1 FROM kanban_sync_outbox').fetchone()
        assert journal.pending_wire(conn) is None
        assert peer.h == peer.a == int(journal.snapshot(conn)['settled'])
        assert peer.h >= 100, 'campaign never reached many-settlement history'
        assert set(peer.fired) == set(CUTS)
        final = custody(conn)
        conn.close()
        conn = kbc.connect(path)
        assert custody(conn) == final
        assert client.push(conn, peer, 'default', 'macbook', primary=False).sent == 0
        assert peer.h == peer.a == len(peer.accepted)
        record_property('campaign', json.dumps(dict(seed=seed, turns=turns, settled=peer.h,
                        cuts=len(peer.fired), reopen_exact=True, transport_rows=1), sort_keys=True))
    finally:
        conn.close()
