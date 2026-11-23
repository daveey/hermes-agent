"""Containment cleanup preserves issued transport until its real final verdict."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_containment_outbox import strip_lifecycle
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


def issued(conn, fields, op='upsert'):
    tid = kb.create_task(conn, title='frozen containment intent', assignee='worker')
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox WHERE table_name != 'tasks'")
        conn.execute('UPDATE tasks SET sync_version=1 WHERE id=?', (tid,))
        conn.execute('UPDATE kanban_sync_outbox SET fields=?,op=?', (json.dumps(fields), op))
    receiver = helpers.Receiver()
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(conn, receiver, 'default', 'macbook', primary=False, max_batches=1)
    return tid, receiver


@pytest.mark.parametrize('fields,op', [({'status': 'ready'}, 'upsert'),
    ({'status': 'ready', 'title': 'retain title'}, 'upsert'), ({}, 'delete')])
def test_cleanup_keeps_issued_identity_bytes_and_recovers_without_resend(live, fields, op):
    tid, receiver = issued(live, fields, op)
    frozen = journal.pending_wire(live)
    row = dict(live.execute('SELECT * FROM kanban_sync_outbox').fetchone())
    with kbc.write_txn(live):
        strip_lifecycle(live, tid)
    assert dict(live.execute('SELECT * FROM kanban_sync_outbox').fetchone()) == row
    assert journal.pending_wire(live) == frozen
    reopened = helpers.reopen(live)
    try:
        receiver.lose_reply = False
        result = client.push(reopened, receiver, 'default', 'macbook', primary=False, max_batches=1)
        assert result.applied == 1 and len(receiver.sent) == 1
        assert journal.pending_wire(reopened) is None and receiver.slot['a'] == '1'
        assert not reopened.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (row['op_id'],)).fetchone()
    finally:
        reopened.close()


def test_issued_hold_is_not_a_task_wide_cleanup_veto(live):
    tid, _ = issued(live, {'status': 'ready', 'title': 'issued'})
    original = dict(live.execute('SELECT * FROM kanban_sync_outbox').fetchone())
    with kbc.write_txn(live):
        live.execute('INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) '
                     "VALUES ('unissued','tasks',?,'upsert',?, '1:0:macbook',1)",
                     (tid, json.dumps({'title': 'later annotation', 'status': 'done'})))
        strip_lifecycle(live, tid)
    rows = [dict(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')]
    assert original in rows and len(rows) == 2
    later, = [row for row in rows if row['op_id'] != original['op_id']]
    assert later['op_id'] != 'unissued' and json.loads(later['fields']) == {'title': 'later annotation'}


@pytest.mark.parametrize('damage', ['anchor', 'wire'])
def test_cleanup_validates_journal_before_any_outbox_edits(live, damage):
    tid, _ = issued(live, {'status': 'ready', 'title': 'issued'})
    with kbc.write_txn(live):
        if damage == 'anchor':
            live.execute("UPDATE kanban_sync_state SET journal_id='wrong'")
        else:
            live.execute("UPDATE kanban_replay_sender1 SET wire=X'7b7d'")
    before = [dict(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')]
    with pytest.raises(ValueError):
        with kbc.write_txn(live):
            strip_lifecycle(live, tid)
    assert [dict(row) for row in live.execute('SELECT * FROM kanban_sync_outbox')] == before


def test_bound_cleanup_requires_caller_transaction(live):
    tid, _ = issued(live, {'status': 'ready'})
    with pytest.raises(ValueError, match='caller transaction required'):
        strip_lifecycle(live, tid)


def test_other_task_cleanup_progresses_while_transport_is_issued(live):
    tid, _ = issued(live, {'status': 'ready'})
    frozen = journal.pending_wire(live)
    other = kb.create_task(live, title='unrelated task', assignee='worker')
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_outbox SET fields=? WHERE table_name='tasks' AND row_uid=?",
                     (json.dumps({'status': 'ready'}), other))
        strip_lifecycle(live, other)
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=?", (other,)).fetchone()
    assert live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=?", (tid,)).fetchone()
    assert journal.pending_wire(live) == frozen


def test_cleanup_after_recorded_rejection_releases_only_transport_hold(live):
    tid, receiver = issued(live, {'status': 'ready', 'title': 'rejected annotation'})
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict['status'] = 'rejected'
    receiver.slot['last_verdict'] = json.dumps(verdict)
    receiver.lose_reply = False
    assert client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1).rejected == 1
    assert journal.pending_wire(live) is None
    with kbc.write_txn(live):
        strip_lifecycle(live, tid)
    row = live.execute("SELECT fields,state FROM kanban_sync_outbox WHERE table_name='tasks'").fetchone()
    assert json.loads(row[0]) == {'title': 'rejected annotation'} and row[1] == 'rejected'


def test_idle_bound_journal_allows_unissued_cleanup(live):
    tid = kb.create_task(live, title='unissued', assignee='worker')
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox WHERE table_name != 'tasks'")
        live.execute('UPDATE kanban_sync_outbox SET fields=?', (json.dumps({'status': 'ready'}),))
        strip_lifecycle(live, tid)
    assert not live.execute('SELECT 1 FROM kanban_sync_outbox').fetchone()
    assert journal.pending_wire(live) is None
