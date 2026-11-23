"""A feed echo is not the authenticated final replay-slot verdict."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_sync as sync, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_lifecycle_custody as helpers
from tests.hermes_cli import test_kanban_replay_sender as sender

live = sender.live


def echo(conn, tid, op_id, *, fields=None):
    return sync.RemoteChange(seq=8, table='tasks', uid=tid, op='upsert',
        fields=fields or {'title': 'frozen containment intent'}, version=8,
        hlc='1:0:macbook', origin_gateway='macbook', client_op_id=op_id)


def test_feed_echo_keeps_exact_issued_source_until_slot_recovery(live):
    tid, receiver = helpers.issued(live, {'title': 'frozen containment intent'})
    original = dict(live.execute('SELECT * FROM kanban_sync_outbox').fetchone())
    frozen = journal.pending_wire(live)
    result = sync.apply_remote_changes(live, [echo(live, tid, original['op_id'])],
                                       new_cursor=8, observers=False)
    row = live.execute('SELECT * FROM kanban_sync_outbox').fetchone()
    assert row is not None, 'feed echo discarded ISSUED source before final verdict'
    assert dict(row) == original
    assert journal.pending_wire(live) == frozen
    assert live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 1
    assert result.skipped_echo == 1
    assert sync.sync_state(live)['cursor'] == 8
    reopened = sender.reopen(live)
    try:
        receiver.lose_reply = False
        settled = client.push(reopened, receiver, 'default', 'macbook', primary=False, max_batches=1)
        assert settled.applied == 1 and len(receiver.sent) == 1
        assert journal.pending_wire(reopened) is None
        assert not reopened.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (original['op_id'],)).fetchone()
    finally:
        reopened.close()


def test_feed_echo_cannot_release_omitted_field_custody(live):
    tid, _ = helpers.issued(live, {'title': 'frozen containment intent'})
    op_id = json.loads(journal.pending_wire(live))['client_op_id']
    with kbc.write_txn(live):
        live.execute('INSERT INTO kanban_sync_omissions '
                     '(original_op_id,table_name,row_uid,field,compat_op_id) '
                     "VALUES ('original','tasks',?,'title',?)", (tid, op_id))
    sync.apply_remote_changes(live, [echo(live, tid, op_id)], new_cursor=8, observers=False)
    assert live.execute('SELECT state FROM kanban_sync_omissions').fetchone()[0] == 'held'


def test_other_echo_and_authoritative_rows_progress_in_same_page(live):
    tid, _ = helpers.issued(live, {'title': 'frozen containment intent'})
    held = json.loads(journal.pending_wire(live))['client_op_id']
    other = kb.create_task(live, title='other local', assignee='worker')
    op_id = live.execute("SELECT op_id FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=?",
                         (other,)).fetchone()[0]
    changes = [echo(live, tid, held), echo(live, other, op_id),
               echo(live, other, None, fields={'title': 'authoritative title'})]
    result = sync.apply_remote_changes(live, changes, new_cursor=8, observers=False)
    assert result.skipped_echo == 2 and result.applied == 1
    assert not live.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (op_id,)).fetchone()
    assert live.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (held,)).fetchone()
    assert live.execute('SELECT title FROM tasks WHERE id=?', (other,)).fetchone()[0] == 'authoritative title'


@pytest.mark.parametrize('damage', ['anchor', 'wire'])
def test_feed_echo_validates_custody_and_rolls_back_page(live, damage):
    tid, _ = helpers.issued(live, {'title': 'frozen containment intent'})
    held = json.loads(journal.pending_wire(live))['client_op_id']
    with kbc.write_txn(live):
        if damage == 'anchor':
            live.execute("UPDATE kanban_sync_state SET journal_id='wrong'")
        else:
            live.execute("UPDATE kanban_replay_sender1 SET wire=X'7b7d'")
    before = [dict(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')]
    cursor = sync.sync_state(live)['cursor']
    with pytest.raises(ValueError):
        sync.apply_remote_changes(live, [echo(live, tid, None, fields={'title': 'must roll back'}),
                                        echo(live, tid, held)], new_cursor=8, observers=False)
    assert [dict(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')] == before
    assert live.execute('SELECT title FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'frozen containment intent'
    assert sync.sync_state(live)['cursor'] == cursor


@pytest.mark.parametrize('bound', [True, False])
def test_unissued_echo_retains_existing_retirement_semantics(live, bound):
    tid = kb.create_task(live, title='unissued task', assignee='worker')
    op_id = live.execute("SELECT op_id FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=?",
                         (tid,)).fetchone()[0]
    if not bound:
        with kbc.write_txn(live):
            live.execute('UPDATE kanban_sync_state SET journal_id=NULL')
            live.execute('DROP TABLE kanban_replay_sender1')
    result = sync.apply_remote_changes(live, [echo(live, tid, op_id)], new_cursor=8, observers=False)
    assert result.skipped_echo == 1
    assert not live.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (op_id,)).fetchone()
    assert live.execute('SELECT sync_version FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 8
