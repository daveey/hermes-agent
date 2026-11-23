"""Controlled deferred handoffs, not live observation or receiver integration proof."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


class Deferred(helpers.Receiver):
    def __init__(self):
        super().__init__()
        self.reference = None
        self.observation = 'origin_pending1'
        self.drop = False

    def _call(self, method, path, body=None):
        reply = super()._call(method, path, body)
        if path.endswith('/capability'):
            reply['deferred_observation'] = self.observation
        if path.endswith('/ops'):
            wire = body['ops'][0]
            self.slot['last_verdict'] = json.dumps(dict(
                client_op_id=wire['client_op_id'], status='deferred', version=99,
                deferred=self.reference or dict(task=wire['uid'], origin=wire['status_origin'])))
            if self.drop:
                raise OSError('deferred reply lost')
        return reply


def push(conn, receiver):
    return client.push(conn, receiver, 'default', 'macbook', primary=False, max_batches=1)


def intent(conn):
    tid = kb.create_task(conn, title='already known task', assignee='worker')
    push(conn, helpers.Receiver())
    # Use real capture of row-before-event, with existing authoritative existence.
    with kbc.write_txn(conn):
        # Isolate status-before-origin ordering from the earlier create event.
        conn.execute('DELETE FROM kanban_sync_outbox')
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'completed', {})
    return tid


def test_deferred_transport_settles_without_status_success_and_event_can_follow(live):
    tid = intent(live)
    receiver = Deferred()
    # Continue the controlled remote slot from the first successful existence intent.
    receiver.slot.update(h='1', a='1', last_op_id=journal.snapshot(live)['last_op_id'],
                         last_payload_sha256=journal.snapshot(live)['last_payload_sha256'],
                         last_verdict=journal.snapshot(live)['last_verdict'])
    result = push(live, receiver)
    assert result.deferred == 1 and result.applied == result.merged == result.rejected == 0
    assert journal.pending_wire(live) is None and receiver.slot['a'] == '2'
    assert live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 7
    from hermes_cli import kanban_replay_deferred as deferred
    assert deferred.pending(live) == [dict(board_incarnation=receiver.slot['board_incarnation'],
        task=tid, origin=receiver.sent[0]['status_origin'], status='done', hlc=receiver.sent[0]['hlc'])]
    receiver.status = 'applied'
    # Use ordinary replies for the origin event: no extra task retry is manufactured.
    receiver._call = helpers.Receiver._call.__get__(receiver)
    result = push(live, receiver)
    assert result.applied == 1 and receiver.sent[-1]['table'] == 'task_events'
    assert len(deferred.pending(live)) == 1  # event transport alone is not queue-resolution proof


def test_lost_deferred_reply_recovers_after_reopen_without_resend(live):
    tid = intent(live)
    receiver = Deferred()
    receiver.slot.update(h='1', a='1', last_op_id=journal.snapshot(live)['last_op_id'],
                         last_payload_sha256=journal.snapshot(live)['last_payload_sha256'],
                         last_verdict=journal.snapshot(live)['last_verdict'])
    receiver.drop = True
    with pytest.raises(OSError, match='deferred reply lost'):
        push(live, receiver)
    frozen = journal.pending_wire(live)
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    live.close()
    reopened = kbc.connect(path)
    try:
        receiver.drop = False
        assert journal.pending_wire(reopened) == frozen
        assert push(reopened, receiver).deferred == 1 and len(receiver.sent) == 1
        from hermes_cli import kanban_replay_deferred as deferred
        assert deferred.pending(reopened)[0]['task'] == tid
        assert receiver.slot['a'] == '2'
    finally:
        reopened.close()


@pytest.mark.parametrize('defect', ['task', 'origin', 'missing', 'capability'])
def test_bad_deferred_handoff_retains_exact_transport(live, defect):
    intent(live)
    receiver = Deferred()
    receiver.slot.update(h='1', a='1', last_op_id=journal.snapshot(live)['last_op_id'],
                         last_payload_sha256=journal.snapshot(live)['last_payload_sha256'],
                         last_verdict=journal.snapshot(live)['last_verdict'])
    receiver.drop = True
    with pytest.raises(OSError, match='deferred reply lost'):
        push(live, receiver)
    wire = journal.pending_wire(live)
    verdict = json.loads(receiver.slot['last_verdict'])
    if defect == 'capability':
        receiver.observation = None
    elif defect == 'missing':
        del verdict['deferred']
    else:
        verdict['deferred'][defect] = 'different-reference'
    receiver.slot['last_verdict'] = json.dumps(verdict)
    receiver.drop = False
    with pytest.raises(ValueError, match='custody integration|deferred reference'):
        push(live, receiver)
    assert journal.pending_wire(live) == wire and receiver.slot['a'] == '1'
    assert journal.snapshot(live)['settled'] == '1' and len(receiver.sent) == 1


def test_deferred_semantic_handoff_and_transport_prune_rollback_together(live, monkeypatch):
    intent(live)
    receiver = Deferred()
    receiver.slot.update(h='1', a='1', last_op_id=journal.snapshot(live)['last_op_id'],
                         last_payload_sha256=journal.snapshot(live)['last_payload_sha256'],
                         last_verdict=journal.snapshot(live)['last_verdict'])
    original = journal.recover

    def fail_after_record(conn, remote, record):
        def fail(verdict):
            record(verdict)
            raise ValueError('after deferred custody')
        return original(conn, remote, fail)

    monkeypatch.setattr(journal, 'recover', fail_after_record)
    with pytest.raises(ValueError, match='after deferred custody'):
        push(live, receiver)
    from hermes_cli import kanban_replay_deferred as deferred
    assert deferred.pending(live) == []
    wire = json.loads(journal.pending_wire(live))
    assert live.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (wire['client_op_id'],)).fetchone()
    assert receiver.slot['a'] == journal.snapshot(live)['settled'] == '1'
    monkeypatch.setattr(journal, 'recover', original)
    assert push(live, receiver).deferred == 1 and len(receiver.sent) == 1
    assert len(deferred.pending(live)) == 1


def test_sixteen_handoffs_do_not_evict_or_reissue_on_local_capacity_fault(live):
    intent(live)
    receiver = Deferred()
    receiver.slot.update(h='1', a='1', last_op_id=journal.snapshot(live)['last_op_id'],
                         last_payload_sha256=journal.snapshot(live)['last_payload_sha256'],
                         last_verdict=journal.snapshot(live)['last_verdict'])
    receiver.drop = True
    with pytest.raises(OSError, match='deferred reply lost'):
        push(live, receiver)
    wire = json.loads(journal.pending_wire(live))
    from hermes_cli import kanban_replay_deferred as deferred
    with kbc.write_txn(live):
        for i in range(16):
            prior = dict(wire, status_origin=f'prior-origin-{i}')
            deferred.handoff(live, prior, dict(deferred=dict(task=prior['uid'], origin=prior['status_origin'])),
                             receiver.slot['board_incarnation'])
    before = deferred.pending(live)
    receiver.drop = False
    with pytest.raises(ValueError, match='fresh membership reconciliation'):
        push(live, receiver)
    assert deferred.pending(live) == before and journal.pending_wire(live) is not None
    assert receiver.slot['a'] == journal.snapshot(live)['settled'] == '1' and len(receiver.sent) == 1
    with kbc.write_txn(live):
        prior = dict(wire, status_origin='prior-origin-0')
        deferred.handoff(live, prior, dict(deferred=dict(task=prior['uid'], origin='prior-origin-0')),
                         receiver.slot['board_incarnation'])
    assert len(deferred.pending(live)) == 16
