"""Full local handoff reconciliation using the proposed future read contract only."""
import json
from urllib.parse import quote

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_replay_deferred as deferred
from hermes_cli import kanban_replay_journal as journal
from tests.hermes_cli import test_kanban_replay_deferred as helpers

live = helpers.live


class Observation(helpers.Deferred):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.reads = 0
        self.reply = {}
        self.task = None

    def _call(self, method, path, body=None):
        if '/deferred?task=' in path:
            assert method == 'GET' and not self.conn.in_transaction
            self.reads += 1
            assert path.endswith(quote(self.task, safe=''))
            return self.reply
        return super()._call(method, path, body)


def full(conn):
    tid = helpers.intent(conn)
    receiver = Observation(conn)
    receiver.task = tid
    receiver.slot.update(h='1', a='1', last_op_id=journal.snapshot(conn)['last_op_id'],
        last_payload_sha256=journal.snapshot(conn)['last_payload_sha256'],
        last_verdict=journal.snapshot(conn)['last_verdict'])
    receiver.drop = True
    with pytest.raises(OSError, match='deferred reply lost'):
        helpers.push(conn, receiver)
    wire = json.loads(journal.pending_wire(conn))
    with kbc.write_txn(conn):
        for i in range(16):
            prior = dict(wire, status_origin=f'prior-origin-{i}')
            deferred.handoff(conn, prior, dict(deferred=dict(task=tid, origin=prior['status_origin'])),
                             receiver.slot['board_incarnation'])
    receiver.drop = False
    receiver.reply = dict(protocol='origin_pending1', board_incarnation=receiver.slot['board_incarnation'],
        gateway_principal=receiver.slot['gateway_principal'], task=tid, complete=True, entries=[
            dict(origin=wire['status_origin'], hlc=wire['hlc'], gateway='other-gateway', run=None,
                 status='done', evidence_sha256='a' * 64)])
    return tid, receiver, wire


def test_full_stale_handoffs_refresh_before_exact_settlement_without_resend(live):
    tid, receiver, wire = full(live)
    result = helpers.push(live, receiver)
    assert receiver.reads == 2 and result.deferred == 1 and len(receiver.sent) == 1
    assert deferred.pending(live) == [dict(board_incarnation=receiver.slot['board_incarnation'],
        task=tid, origin=wire['status_origin'], status='done', hlc=wire['hlc'])]
    assert journal.pending_wire(live) is None and receiver.slot['a'] == '2'
    assert live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 7


def test_membership_removal_rolls_back_with_handoff_and_journal(live, monkeypatch):
    _, receiver, _ = full(live)
    before = deferred.pending(live)
    frozen = journal.pending_wire(live)
    original = deferred.handoff

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError('after refreshed handoff')

    monkeypatch.setattr(deferred, 'handoff', fail)
    with pytest.raises(ValueError, match='after refreshed handoff'):
        helpers.push(live, receiver)
    assert deferred.pending(live) == before and journal.pending_wire(live) == frozen
    assert receiver.slot['a'] == '1' and len(receiver.sent) == 1


@pytest.mark.parametrize('defect', ['protocol', 'board_incarnation', 'gateway_principal', 'task',
    'complete', 'entries', 'duplicate', 'overflow', 'origin', 'hlc', 'gateway', 'run',
    'status', 'evidence_sha256'])
def test_invalid_observation_never_retires_custody(live, defect):
    _, receiver, _ = full(live)
    before, frozen = deferred.pending(live), journal.pending_wire(live)
    changes = {key: (lambda key=key: receiver.reply.update({key: 'wrong'}))
               for key in ('protocol', 'board_incarnation', 'gateway_principal', 'task')}
    changes.update({
        'complete': lambda: receiver.reply.update(complete=1),
        'entries': lambda: receiver.reply.update(entries={}),
        'duplicate': lambda: receiver.reply.update(entries=receiver.reply['entries'] * 2),
        'overflow': lambda: receiver.reply.update(entries=[dict(receiver.reply['entries'][0],
                                    origin=f'origin-{i}') for i in range(17)]),
    })
    for key in ('origin', 'hlc', 'gateway', 'run', 'status', 'evidence_sha256'):
        changes[key] = lambda key=key: receiver.reply['entries'][0].update({key: []})
    changes[defect]()
    reasons = dict(complete='completeness', duplicate='duplicate origin', overflow='entries',
                   evidence_sha256='fingerprint')
    with pytest.raises(ValueError, match=f'^invalid deferred observation {reasons.get(defect, defect)}$'):
        helpers.push(live, receiver)
    assert deferred.pending(live) == before and journal.pending_wire(live) == frozen
    assert receiver.slot['a'] == '1' and len(receiver.sent) == 1


def test_concurrent_handoff_during_read_cannot_be_retired_by_older_observation(live):
    _, receiver, wire = full(live)
    original = receiver._call

    def race(method, path, body=None):
        reply = original(method, path, body)
        if '/deferred?task=' in path and receiver.reads == 1:
            with kbc.write_txn(live):
                newer = dict(wire, status_origin='prior-origin-0', hlc='newer-captured-hlc')
                deferred.handoff(live, newer, dict(deferred=dict(task=wire['uid'], origin='prior-origin-0')),
                                 receiver.slot['board_incarnation'])
            # The later ordinary poll sees the newer remote obligation too.
            receiver.reply = dict(reply, entries=reply['entries'] + [dict(reply['entries'][0],
                                  origin='prior-origin-0', hlc='newer-captured-hlc')])
        return reply

    receiver._call = race
    helpers.push(live, receiver)
    remaining = deferred.pending(live)
    assert any(item['origin'] == 'prior-origin-0' and item['hlc'] == 'newer-captured-hlc' for item in remaining)


def test_unavailable_read_recovers_exact_pending_without_resend(live):
    _, receiver, _ = full(live)
    original = receiver._call
    before, frozen = deferred.pending(live), journal.pending_wire(live)

    def unavailable(method, path, body=None):
        if '/deferred?task=' in path:
            raise OSError('observation unavailable')
        return original(method, path, body)

    receiver._call = unavailable
    with pytest.raises(OSError, match='observation unavailable'):
        helpers.push(live, receiver)
    assert deferred.pending(live) == before and journal.pending_wire(live) == frozen
    receiver._call = original
    assert helpers.push(live, receiver).deferred == 1 and len(receiver.sent) == 1


def test_complete_full_membership_does_not_evict_an_origin(live):
    _, receiver, _ = full(live)
    template = receiver.reply['entries'][0]
    receiver.reply['entries'] = [dict(template, origin=f'prior-origin-{i}') for i in range(16)]
    before, frozen = deferred.pending(live), journal.pending_wire(live)
    with pytest.raises(ValueError, match='fresh membership reconciliation'):
        helpers.push(live, receiver)
    assert deferred.pending(live) == before and journal.pending_wire(live) == frozen
    assert receiver.reads == 1 and len(receiver.sent) == 1


def test_complete_empty_membership_never_marks_status_success(live):
    tid, receiver, _ = full(live)
    receiver.reply['entries'] = []
    assert helpers.push(live, receiver).deferred == 1
    # A separate complete post-settlement read retires the diagnostic reference;
    # absence is not application and no remote/current-row status is synthesized.
    assert receiver.reads == 2 and deferred.pending(live) == []
    assert live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 7
