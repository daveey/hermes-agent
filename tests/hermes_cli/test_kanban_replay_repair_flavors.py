"""Physical flavor and transient repair remain fenced through the actual sender."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli import test_kanban_replay_primary as primary
from tests.hermes_cli import test_kanban_replay_repair as repair

live = helpers.live


@pytest.mark.parametrize('physical,changed', [('upsert', 'compat'), ('import', 'compat_import'),
                                               ('compat', 'upsert'), ('compat_import', 'import')])
def test_physical_source_kind_is_pinned_across_lost_reply_reopen(live, physical, changed):
    kb.create_task(live, title='physical custody', assignee='worker')
    original = live.execute("SELECT op_id FROM kanban_sync_outbox WHERE table_name='tasks'").fetchone()[0]
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_outbox SET op=? WHERE op_id=?', (physical, original))
    receiver = helpers.Receiver()
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    frozen = journal.pending_wire(live)
    assert journal.snapshot(live)['pending']['source_op'] == physical
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_outbox SET op=? WHERE op_id=?', (changed, original))
    reopened = helpers.reopen(live)
    receiver.lose_reply = False
    try:
        with pytest.raises(ValueError, match='source custody routing changed'):
            client.push(reopened, receiver, 'default', 'macbook', primary=False, max_batches=1)
        assert journal.pending_wire(reopened) == frozen
        assert receiver.slot['a'] == '0' and len(receiver.sent) == 1
    finally:
        reopened.close()


def test_primary_transient_refusal_does_not_read_or_overwrite_local_intent(live):
    tid = kb.create_task(live, title='unsynced edit survives refusal', assignee='worker')
    receiver = primary.Primary(live)
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict.update(status='rejected', reason='storage_error')
    receiver.slot['last_verdict'] = json.dumps(verdict)
    receiver.lose_reply = False
    receiver.evidence = dict(board='default', table='tasks', uid=tid, state='live', head=99,
                             row=dict(uid=tid, title='stale remote title', status='todo', version=19))
    reopened = helpers.reopen(live)
    receiver.conn = reopened
    try:
        client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1)
        assert reopened.execute('SELECT title FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'unsynced edit survives refusal'
        assert receiver.reads == [] and receiver.slot['a'] == '1'
        assert reopened.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid=? AND state='pending'", (tid,)).fetchone()
    finally:
        reopened.close()


def test_uncaptured_import_fails_before_allocation_not_as_an_issued_wedge(live):
    kb.create_task(live, title='unknown historical status', assignee='worker')
    original = live.execute("SELECT op_id FROM kanban_sync_outbox WHERE table_name='tasks'").fetchone()[0]
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_outbox SET op='import',status_origin=NULL WHERE op_id=?", (original,))
    receiver = helpers.Receiver()
    before = repair.row(live, original)
    with pytest.raises(ValueError, match='uncaptured source status'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert journal.pending_wire(live) is None and journal.snapshot(live)['settled'] == '0'
    assert repair.row(live, original) == before and receiver.sent == []


@pytest.mark.parametrize('mode', [False, True])
def test_synthetic_parent_transient_refusal_retains_child_and_queues_bounded_retry(live, mode):
    tid = kb.create_task(live, title='synthetic parent', assignee='worker')
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox WHERE table_name='tasks'")
    receiver = primary.Primary(live)
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(live, receiver, 'default', 'macbook', primary=mode, max_batches=1)
    original = receiver.sent[0]['client_op_id']
    assert receiver.sent[0]['uid'] == tid and receiver.sent[0]['table'] == 'tasks'
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict.update(status='rejected', reason='storage_error')
    receiver.slot['last_verdict'] = json.dumps(verdict)
    receiver.lose_reply = False
    children = [dict(r) for r in live.execute('SELECT * FROM kanban_sync_outbox')]
    client.push(live, receiver, 'default', 'macbook', primary=mode, max_batches=1)
    retry = live.execute("SELECT * FROM kanban_sync_outbox WHERE table_name='tasks' AND state='pending'").fetchone()
    assert retry is not None and retry['op_id'] != original and retry['attempts'] == 1
    assert all(repair.row(live, child['op_id']) == child for child in children)
    assert journal.pending_wire(live) is None and receiver.slot['a'] == '1' and len(receiver.sent) == 1
