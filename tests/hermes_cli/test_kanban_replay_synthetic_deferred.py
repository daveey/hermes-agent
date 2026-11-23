"""Synthetic deferred custody is separate from authoritative task existence. [daveey]"""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_deferred as deferred
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_deferred as handoffs
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


class SyntheticDeferred(handoffs.Deferred):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.evidence = None
        self.reads = 0

    def row_evidence(self, board, table, uid):
        assert not self.conn.in_transaction
        self.reads += 1
        return self.evidence


def issue(conn):
    tid = kb.create_task(conn, title='synthetic original', assignee='worker')
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'completed', {})
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox WHERE table_name!='task_events'")
        # Isolate the origin event which follows the deferred synthetic status.
        conn.execute("DELETE FROM kanban_sync_outbox WHERE table_name='task_events' "
                     "AND json_extract(fields,'$.kind')!='completed'")
    receiver = SyntheticDeferred(conn)
    receiver.drop = True
    with pytest.raises(OSError, match='deferred reply lost'):
        handoffs.push(conn, receiver)
    receiver.drop = False
    receiver.evidence = dict(board='default', table='tasks', uid=tid, state='live',
                             head=91, row=dict(uid=tid, id=tid, title='remote current',
                                               body='remote body', status='todo', version=19))
    assert receiver.sent[0]['status_origin']
    assert receiver.sent[0]['fields']['status'] == 'done'
    return tid, receiver


def recover_without_unsupported_exception(conn, receiver):
    try:
        return handoffs.push(conn, receiver)
    except ValueError as exc:
        if 'tracer final disposition requires custody integration' not in str(exc):
            raise
        return None


@pytest.mark.parametrize('later_pending', [False, True])
def test_synthetic_deferred_reopen_hands_off_origin_and_releases_event_not_parent(live, later_pending):
    tid, receiver = issue(live)
    wire = json.loads(journal.pending_wire(live))
    frozen = receiver.slot['last_verdict']
    reopened = helpers.reopen(live)
    receiver.conn = reopened
    try:
        if later_pending:
            with kbc.write_txn(reopened):
                reopened.execute('UPDATE tasks SET title=? WHERE id=?', ('later local title', tid))
        result = recover_without_unsupported_exception(reopened, receiver)
        assert deferred.pending(reopened) == [dict(board_incarnation=receiver.slot['board_incarnation'],
            task=tid, origin=wire['status_origin'], status='done', hlc=wire['hlc'])], 'synthetic origin handoff missing'
        assert result.deferred == 1 and result.applied == result.merged == 0 and result.sent == 0
        row = reopened.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        assert row['sync_version'] == 19 and row['sync_version'] != 99
        # Existing terminal arbitration preserves local lifecycle evidence; the
        # deferred verdict's version/status is never used as a success receipt.
        assert row['status'] == 'done' and row['body'] == 'remote body'
        assert row['title'] == ('later local title' if later_pending else 'remote current')
        assert journal.snapshot(reopened)['last_verdict'] == frozen
        assert journal.pending_wire(reopened) is None and receiver.slot['a'] == '1'
        assert receiver.reads == 1 and len(receiver.sent) == 1
        assert not reopened.execute('SELECT 1 FROM kanban_sync_parent_envelopes').fetchone()
        receiver._call = helpers.Receiver._call.__get__(receiver)
        assert handoffs.push(reopened, receiver).applied == 1
        assert receiver.sent[-1]['table'] == 'task_events', 'synthetic parent was regenerated'
        assert receiver.sent[-1]['fields']['kind'] == 'completed'
        assert len(deferred.pending(reopened)) == 1  # event transport does not prove status applied
    finally:
        reopened.close()


@pytest.mark.parametrize('defect', ['unknown', 'deleted', 'local_delete', 'foreign'])
def test_unproven_existence_cannot_release_synthetic_deferred_or_child(live, defect):
    tid, receiver = issue(live)
    if defect == 'local_delete':
        with kbc.write_txn(live):
            live.execute("INSERT INTO kanban_sync_tombstones VALUES ('tasks',?,1)", (tid,))
    elif defect == 'foreign':
        receiver.evidence['board'] = 'foreign'
    else:
        receiver.evidence['state'] = defect
        receiver.evidence['row'] = None
    frozen = journal.pending_wire(live)
    with pytest.raises(ValueError):
        handoffs.push(live, receiver)
    assert journal.pending_wire(live) == frozen and receiver.slot['a'] == '0'
    assert journal.snapshot(live)['settled'] == '0'
    wire = json.loads(frozen)
    assert deferred.pending(live) == [dict(board_incarnation=receiver.slot['board_incarnation'],
        task=tid, origin=wire['status_origin'], status='done', hlc=wire['hlc'])]
    assert live.execute('SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()
    assert len(receiver.sent) == 1


def test_projection_failure_preserves_known_handoff_but_rolls_back_parent_retirement(live, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    tid, receiver = issue(live)
    frozen = journal.pending_wire(live)
    original = sync.apply_remote_changes

    def fail_after_projection(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('after synthetic deferred projection')

    monkeypatch.setattr(sync, 'apply_remote_changes', fail_after_projection)
    with pytest.raises(OSError, match='after synthetic deferred projection'):
        handoffs.push(live, receiver)
    wire = json.loads(frozen)
    assert deferred.pending(live) == [dict(board_incarnation=receiver.slot['board_incarnation'],
        task=tid, origin=wire['status_origin'], status='done', hlc=wire['hlc'])]
    assert journal.pending_wire(live) == frozen
    assert receiver.slot['a'] == journal.snapshot(live)['settled'] == '0'
    row = live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    assert row['title'] == 'synthetic original' and row['sync_version'] is None
    monkeypatch.setattr(sync, 'apply_remote_changes', original)
    assert handoffs.push(live, receiver).deferred == 1 and len(receiver.sent) == 1
