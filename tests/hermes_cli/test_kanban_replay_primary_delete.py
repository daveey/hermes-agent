"""Primary delete settlement requires authenticated absence, not an impossible live row."""
import copy

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_replay_existence as existence
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers
from tests.hermes_cli.test_kanban_replay_primary import Primary

live = helpers.live


def pending_delete(conn):
    a = kb.create_task(conn, title='a', assignee='worker')
    b = kb.create_task(conn, title='b', assignee='worker')
    kb.link_tasks(conn, a, b)
    receiver = Primary(conn)
    receiver.status = 'applied'
    for _ in range(60):
        if not conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'").fetchone() \
                and journal.pending_wire(conn) is None:
            break
        client.push(conn, receiver, 'default', 'macbook', primary=False, max_batches=5)
    else:
        raise AssertionError('initial real outbox not drained')
    kb.unlink_tasks(conn, a, b)
    row = dict(conn.execute("SELECT * FROM kanban_sync_outbox WHERE state='pending' AND op='delete'").fetchone())
    assert row['table_name'] == 'task_links'
    receiver.evidence = dict(board='default', table=row['table_name'], uid=row['row_uid'],
                             state='deleted', head=5, row=None)
    return receiver, row


@pytest.mark.parametrize('lost_reply', [False, True])
@pytest.mark.parametrize('status', ['applied', 'merged'])
def test_primary_applied_real_delete_settles_and_acknowledges_once(live, lost_reply, status):
    receiver, row = pending_delete(live)
    receiver.status = status
    before = len(receiver.sent)
    receiver.lose_reply = lost_reply
    if lost_reply:
        with pytest.raises(OSError, match='reply lost'):
            client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    reopened = helpers.reopen(live)
    receiver.conn = reopened
    receiver.lose_reply = False
    try:
        result = client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1)
        assert getattr(result, status) == 1 and result.sent == int(not lost_reply)
        assert len(receiver.sent) == before + 1
        assert receiver.sent[-1]['op'] == 'delete' and receiver.sent[-1]['client_op_id'] == row['op_id']
        assert journal.pending_wire(reopened) is None and existence.fault(reopened) is None
        assert receiver.slot['a'] == journal.snapshot(reopened)['settled']
        assert not reopened.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (row['op_id'],)).fetchone()
        assert reopened.execute('SELECT COUNT(*) FROM task_links').fetchone()[0] == 0
        client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=0)
        assert len(receiver.sent) == before + 1
    finally:
        reopened.close()


@pytest.mark.parametrize('key,value', [
    ('board', 'foreign'), ('table', 'tasks'), ('uid', 'foreign'),
    ('head', True), ('head', -1), ('head', None),
    ('state', 'unknown'), ('state', 'never'), ('row', {}),
])
def test_deleted_evidence_must_be_bound_and_authoritative(live, key, value):
    receiver, row = pending_delete(live)
    valid = copy.deepcopy(receiver.evidence)
    receiver.evidence[key] = value
    with pytest.raises(existence.CurrentRowHold):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    wire = journal.pending_wire(live)
    assert wire is not None and journal.snapshot(live)['pending']['op_id'] == row['op_id']
    assert live.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (row['op_id'],)).fetchone()
    held = existence.fault(live)
    assert held['paused'] == 1
    reopened = helpers.reopen(live)
    receiver.conn = reopened
    receiver.evidence = valid
    before = len(receiver.sent)
    try:
        assert journal.pending_wire(reopened) == wire
        existence.resume(reopened, row['op_id'], 'verified current deletion evidence')
        assert client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1).applied == 1
        assert journal.pending_wire(reopened) is None and len(receiver.sent) == before
    finally:
        reopened.close()
