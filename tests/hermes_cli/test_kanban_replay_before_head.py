"""Pre-head selection/recovery through the real bound-journal push entry."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


def run_intent(conn):
    tid = kb.create_task(conn, title='pre-head parent', assignee='worker')
    with kbc.write_txn(conn):
        conn.execute('UPDATE tasks SET sync_version=2 WHERE id=?', (tid,))
        conn.execute('DELETE FROM kanban_sync_outbox')
        conn.execute("INSERT INTO task_runs(task_id,status,started_at,uid) VALUES (?,'running',1,'run-pre-head')", (tid,))
    return tid


def push(conn, receiver, *, before_head=True, seen=None):
    return client.push(conn, receiver, 'default', 'macbook', primary=False,
                       before_head=before_head, seen=seen, max_batches=1)


def test_pre_head_sends_safe_task_without_legacy_fallback(live):
    tid = kb.create_task(live, title='safe create before head', assignee='worker')
    receiver = helpers.Receiver()
    result = push(live, receiver)
    assert result.applied == 1 and receiver.sent[0]['uid'] == tid
    assert receiver.slot['a'] == '1' and journal.pending_wire(live) is None


def test_pre_head_holds_unissued_run_without_allocating(live):
    run_intent(live)
    receiver = helpers.Receiver()
    result = push(live, receiver)
    assert result.sent == result.applied == 0 and not receiver.sent
    assert journal.snapshot(live)['pending'] is None
    assert live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='task_runs'").fetchone()
    assert push(live, receiver, before_head=False).applied == 1


@pytest.mark.parametrize('committed', [False, True])
def test_pre_head_reopen_records_known_run_but_holds_uncertain_retry(live, committed, monkeypatch):
    run_intent(live)
    receiver = helpers.Receiver() if committed else helpers.PrecommitFailure()
    receiver.lose_reply = True
    with pytest.raises(OSError):
        push(live, receiver, before_head=False)
    frozen = journal.pending_wire(live)
    assert json.loads(frozen)['table'] == 'task_runs'
    restarted = helpers.reopen(live)
    resumed = helpers.Receiver()
    resumed.slot = dict(receiver.slot)
    try:
        result = push(restarted, resumed)
        assert result.sent == 0 and not resumed.sent
        if committed:
            assert result.applied == 1 and resumed.slot['a'] == '1'
            assert journal.pending_wire(restarted) is None
        else:
            assert result.applied == 0 and resumed.slot['a'] == '0'
            assert journal.pending_wire(restarted) == frozen
            helpers.retry_due(restarted, monkeypatch)
            assert push(restarted, resumed, before_head=False).applied == 1
            assert len(resumed.sent) == 1
    finally:
        restarted.close()


def test_pre_head_reuses_round_terminal_read_cache(live):
    tid = kb.create_task(live, title='stale wake before head', assignee='worker')
    with kbc.write_txn(live):
        live.execute('UPDATE tasks SET sync_version=2 WHERE id=?', (tid,))
        live.execute("DELETE FROM kanban_sync_outbox WHERE table_name!='tasks'")
    class Terminal(helpers.Receiver):
        reads = 0
        def row_evidence(self, board, table, uid):
            assert not live.in_transaction
            self.reads += 1
            return {'row': {'uid': uid, 'status': 'done'}}
    receiver = Terminal()
    seen = {}
    for _ in range(2):
        assert push(live, receiver, seen=seen).sent == 0
    assert receiver.reads == 1 and seen == {tid: True}
    assert journal.pending_wire(live) is None and not receiver.sent


def test_pre_head_retry_checks_frozen_status_not_rewritten_source_fields(live, monkeypatch):
    tid = kb.create_task(live, title='frozen stale wake', assignee='worker')
    with kbc.write_txn(live):
        live.execute('UPDATE tasks SET sync_version=2 WHERE id=?', (tid,))
        live.execute("DELETE FROM kanban_sync_outbox WHERE table_name!='tasks'")
    receiver = helpers.PrecommitFailure()
    with pytest.raises(OSError):
        push(live, receiver, before_head=False)
    frozen = journal.pending_wire(live)
    wire = json.loads(frozen)
    assert wire['fields']['status'] not in ('done', 'archived', 'running')
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_outbox SET fields=? WHERE op_id=?",
                     (json.dumps({'status': 'done'}), wire['client_op_id']))
    class Terminal(helpers.Receiver):
        def row_evidence(self, board, table, uid):
            assert not live.in_transaction
            return {'row': {'uid': uid, 'status': 'done'}}
    resumed = Terminal()
    resumed.slot = dict(receiver.slot)
    assert push(live, resumed).sent == 0
    assert not resumed.sent and journal.pending_wire(live) == frozen
    helpers.retry_due(live, monkeypatch)
    assert push(live, resumed, before_head=False).applied == 1


def test_pre_head_checks_saved_unissued_envelope_before_transmission(live):
    from hermes_cli import kanban_sync_envelope as envelopes
    from hermes_cli import kanban_sync_wire as wire_ops
    tid = kb.create_task(live, title='saved unissued wake', assignee='worker')
    with kbc.write_txn(live):
        live.execute('UPDATE tasks SET sync_version=2 WHERE id=?', (tid,))
        live.execute("DELETE FROM kanban_sync_outbox WHERE table_name!='tasks'")
        row = dict(live.execute('SELECT * FROM kanban_sync_outbox').fetchone())
        saved = envelopes.outbound(live, row, lambda: wire_ops.translate(row, client._next_hlc(live, 'macbook')))
        live.execute('UPDATE kanban_sync_outbox SET fields=? WHERE op_id=?',
                     (json.dumps({'status': 'done'}), row['op_id']))
    class Terminal(helpers.Receiver):
        def row_evidence(self, board, table, uid):
            assert not live.in_transaction
            return {'row': {'uid': uid, 'status': 'done'}}
    receiver = Terminal()
    assert push(live, receiver).sent == 0
    assert not receiver.sent
    issued = json.loads(journal.pending_wire(live))
    issued.pop('replay')
    assert issued == saved
