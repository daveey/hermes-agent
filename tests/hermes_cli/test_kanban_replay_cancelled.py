"""Final cancellation is visible non-success custody, not a successful row apply."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


class Cancelled(helpers.Receiver):
    def __init__(self):
        super().__init__()
        self.status = 'cancelled'
        self.reason = None

    def _call(self, method, path, body=None):
        reply = super()._call(method, path, body)
        if path.endswith('/ops'):
            verdict = json.loads(self.slot['last_verdict'])
            verdict.update(reason=self.reason, detail='explicit owner cancellation')
            self.slot['last_verdict'] = json.dumps(verdict)
        return reply


def push(conn, receiver):
    return client.push(conn, receiver, 'default', 'macbook', primary=False, max_batches=1)


def retained(conn, receiver):
    wire = dict(receiver.sent[0])
    del wire['replay']
    row = conn.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (wire['client_op_id'],)).fetchone()
    assert row is not None and row['state'] == 'rejected'
    assert row['last_error'].startswith('cancelled:')
    assert row['attempts'] == 1
    assert json.loads(row['wire_json']) == wire
    assert journal.pending_wire(conn) is None
    assert journal.snapshot(conn)['settled'] == '1'
    assert json.loads(journal.snapshot(conn)['last_verdict'])['status'] == 'cancelled'
    assert receiver.slot['a'] == '1'
    return dict(row)


@pytest.mark.parametrize('reason', [None, 'forbidden_field', 'x' * 600], ids=['no-reason', 'reason', 'bounded'])
def test_cancelled_retains_exact_custody_without_success_and_holds_import(live, reason):
    tid = kb.create_task(live, title='cancelled intent', assignee='worker')
    receiver = Cancelled()
    receiver.reason = reason
    result = push(live, receiver)
    row = retained(live, receiver)
    assert row['last_error'] == f"cancelled: {reason or 'cancelled'}: explicit owner cancellation"[:500]
    assert result.cancelled == 1
    assert result.applied == result.merged == result.rejected == result.deferred == 0
    assert live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] is None
    with kbc.write_txn(live), pytest.raises(ValueError, match='import overlaps replay custody'):
        journal.guard_import(live, ('tasks',), {'tasks': {tid}})
    # Settlement is transport-only: later ordinary work remains selectable.
    other = kb.create_task(live, title='unrelated intent', assignee='worker')
    receiver.status = 'applied'
    result = push(live, receiver)
    assert result.applied == 1 and receiver.sent[-1]['uid'] == other
    assert dict(live.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (row['op_id'],)).fetchone()) == row


def test_postcommit_loss_recovers_cancellation_after_reopen_without_resend(live):
    kb.create_task(live, title='cancelled lost reply', assignee='worker')
    receiver = Cancelled()
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        push(live, receiver)
    path = Path(live.execute('PRAGMA database_list').fetchone()[2])
    frozen = journal.pending_wire(live)
    live.close()
    restarted = kbc.connect(path)
    try:
        assert journal.pending_wire(restarted) == frozen
        receiver.lose_reply = False
        result = push(restarted, receiver)
        assert result.cancelled == 1 and len(receiver.sent) == 1
        row = retained(restarted, receiver)
        again = client.push(restarted, receiver, 'default', 'macbook', primary=False, max_batches=0)
        assert again.cancelled == 0
        assert dict(restarted.execute('SELECT * FROM kanban_sync_outbox WHERE op_id=?', (row['op_id'],)).fetchone()) == row
    finally:
        restarted.close()


def test_cancelled_synthetic_parent_moves_exact_wire_and_holds_child(live):
    tid = kb.create_task(live, title='synthetic cancelled parent', assignee='worker')
    with kbc.write_txn(live):
        live.execute('DELETE FROM kanban_sync_outbox')
    kb.add_comment(live, tid, author='worker', body='child must wait')
    receiver = Cancelled()
    result = push(live, receiver)
    row = retained(live, receiver)
    assert result.cancelled == 1 and row['row_uid'] == tid
    assert not live.execute('SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()
    assert live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] is None
    receiver.status = 'applied'
    assert push(live, receiver).sent == 0
    assert len(receiver.sent) == 1


def test_cancellation_record_and_parent_retirement_roll_back_together(live, monkeypatch):
    tid = kb.create_task(live, title='rollback cancelled parent', assignee='worker')
    with kbc.write_txn(live):
        live.execute('DELETE FROM kanban_sync_outbox')
    kb.add_comment(live, tid, author='worker', body='retained child')
    receiver = Cancelled()
    original = journal.recover

    def fail_after_record(conn, remote, record):
        def fail(verdict):
            record(verdict)
            raise ValueError('after cancellation custody write')
        return original(conn, remote, fail)

    monkeypatch.setattr(journal, 'recover', fail_after_record)
    with pytest.raises(ValueError, match='after cancellation custody write'):
        push(live, receiver)
    assert journal.pending_wire(live) is not None
    assert journal.snapshot(live)['settled'] == receiver.slot['a'] == '0'
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='rejected'").fetchone()
    assert live.execute('SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()
    monkeypatch.setattr(journal, 'recover', original)
    result = push(live, receiver)
    assert result.cancelled == 1 and len(receiver.sent) == 1
    retained(live, receiver)
