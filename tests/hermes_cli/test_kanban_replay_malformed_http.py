"""Malformed Board replies through real Hub HTTP; not deployed interoperability.

The fixture accepts one operation, then serves unusable evidence. No hostile
reply is permission to settle or resend. Only separately repaired authoritative
inspection can recover the exact retained intent after SQLite reopen.
"""
import copy
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_http_session as http
from tests.hermes_cli.test_kanban_replay_state_model import custody

live = http.live

# Each row changes a real response rather than stubbing validation or settlement.
OPS = {
    'bad-protocol': lambda r: dict(r, protocol='foreign1'),
    'bad-envelope': lambda r: [],
    'bad-results': lambda r: dict(r, results={}),
    'bad-result': lambda r: dict(r, results=[None]),
    'truncated-results': lambda r: dict(r, results=[]),
    'duplicate-results': lambda r: dict(r, results=r['results'] * 2),
    'out-of-sequence': lambda r: change(r, sequence='2'),
    'regressed-prefix': lambda r: change(r, h='0'),
    'numeric-sequence': lambda r: change(r, sequence=1),
    'bad-digest': lambda r: change(r, payload_sha256='f' * 64),
    'foreign-op': lambda r: change(r, client_op_id='foreign-operation'),
    'not-consumed': lambda r: change(r, consumed=False),
    'numeric-consumed': lambda r: change(r, consumed=1),
    'unknown-outcome': lambda r: change(r, outcome_unknown=True),
    'duplicate-flag': lambda r: change(r, dup=True),
    'bad-verdict': lambda r: change(r, status='already_processed'),
    'truncated-digest': lambda r: without(r, 'payload_sha256'),
    'truncated-status': lambda r: without(r, 'status'),
    'truncated-json': lambda r: json.dumps(r).encode()[:-1],
    'truncated-utf8': lambda r: b'{"results": "\xc3',
}
SLOTS = {
    'bad-mode': lambda s: dict(s, mode='LEGACY'),
    'truncated-binding': lambda s: {k: v for k, v in s.items() if k != 'journal_id'},
    'foreign-binding': lambda s: dict(s, journal_id='foreign-journal'),
    'out-of-sequence': lambda s: dict(s, h='2', a='1'),
    'numeric-prefix': lambda s: dict(s, h=1),
    'duplicate-window': lambda s: dict(s, a='2'),
    'bad-retained-json': lambda s: dict(s, last_verdict=s['last_verdict'][:-1]),
    'bad-retained-status': lambda s: retained(s, status='outcome_unknown'),
    'foreign-retained-id': lambda s: retained(s, client_op_id='foreign-operation'),
}


def change(reply, **fields):
    reply = copy.deepcopy(reply)
    reply['results'][0].update(fields)
    return reply


def without(reply, field):
    reply = copy.deepcopy(reply)
    del reply['results'][0][field]
    return reply


def retained(slot, **fields):
    verdict = json.loads(slot['last_verdict'])
    verdict.update(fields)
    return dict(slot, last_verdict=json.dumps(verdict))


class Damaged(http.AckReceiver):
    def __init__(self, case, *, slot=False):
        super().__init__()
        self.case = case
        self.slot_case = slot
        self.repaired = False

    def _call(self, method, path, body=None):
        reply = super()._call(method, path, body)
        if not self.repaired and self.sent:
            if path.endswith('/ops') and self.slot_case:
                return dict(protocol='processed_prefix1', results=[])
            if path.endswith('/inspect'):
                # An ops-shape refusal must inspect, not convert a different
                # source of unusable evidence into a fabricated success.
                transform = SLOTS[self.case] if self.slot_case else SLOTS['bad-mode']
                return dict(slot=transform(reply['slot']))
        return reply

    def encode(self, path, reply):
        if path.endswith('/ops') and not self.repaired and not self.slot_case:
            reply = OPS[self.case](reply)
        return reply if isinstance(reply, bytes) else json.dumps(reply).encode()


def push(conn, hub):
    return client.push(conn, hub, 'default', 'macbook', primary=False, max_batches=1)


def check_recovery(conn, peer, requests, hub, tid):
    frozen = custody(conn)
    assert frozen[1] is not None, 'malformed reply settled uncertain operation'
    assert journal.snapshot(conn)['settled'] == '0'
    assert peer.slot['h'] == '1' and peer.slot['a'] == '0'
    assert len(peer.sent) == 1 and 'ack' not in peer.calls
    assert conn.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] is None
    wire = json.loads(frozen[1])
    accepted = copy.deepcopy(peer.sent[0])
    accepted['replay'].pop('session')
    assert wire == accepted
    original = conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (wire['client_op_id'],)).fetchone()
    assert original is not None
    path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
    with kbc.connect(path) as reopened:
        assert custody(reopened) == frozen
        with pytest.raises(ValueError):
            push(reopened, hub)
        assert custody(reopened) == frozen, 'bad reopened inspection changed custody'
        assert len(peer.sent) == 1 and 'ack' not in peer.calls
        peer.repaired = True
        result = push(reopened, hub)
        assert result.applied == 1 and result.sent == 0
        assert journal.pending_wire(reopened) is None
        assert journal.snapshot(reopened)['settled'] == peer.slot['a'] == '1'
        assert not reopened.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?',
                                    (wire['client_op_id'],)).fetchone()
        assert reopened.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 7
    assert len(peer.sent) == 1
    assert len([p for _, p, _, _ in requests if p.endswith('/ops')]) == 1


@pytest.mark.parametrize('case', list(OPS))
def test_malformed_ops_refuses_or_inspects_without_settlement_or_resend(live, case):
    tid = kb.create_task(live, title='malformed ops reply', assignee='worker')
    peer = Damaged(case)
    with http.serving(live, peer, encode_reply=peer.encode) as (url, requests, observations):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            with pytest.raises(ValueError):
                push(live, hub)
            check_recovery(live, peer, requests, hub, tid)
        finally:
            hub.close()
    assert len(observations) == 1


@pytest.mark.parametrize('case', list(SLOTS))
def test_malformed_inspection_never_releases_pending_custody(live, case):
    tid = kb.create_task(live, title='malformed retained slot', assignee='worker')
    peer = Damaged(case, slot=True)
    with http.serving(live, peer, encode_reply=peer.encode) as (url, requests, observations):
        hub = client.Hub(client.Identity(url, 'fixture-key', 'display'))
        try:
            with pytest.raises(ValueError):
                push(live, hub)
            check_recovery(live, peer, requests, hub, tid)
        finally:
            hub.close()
    assert len(observations) == 1
