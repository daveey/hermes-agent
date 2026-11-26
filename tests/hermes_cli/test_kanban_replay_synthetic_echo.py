"""Issued synthetic-parent feed echo vs settlement: the merged image must survive."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client, kanban_replay_journal as journal
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


@pytest.mark.parametrize('echo_before_settle', [False, True])
def test_synthetic_merged_echo_keeps_canonical_image(live, echo_before_settle):
    tid = kb.create_task(live, title='local title', assignee='worker')
    with kbc.write_txn(live):
        live.execute('DELETE FROM kanban_sync_outbox')
    kb.add_comment(live, tid, 'author', 'child')
    class Merged(helpers.Receiver):
        def row_evidence(self, board, table, uid):
            return dict(board=board, table=table, uid=uid, state='live', head=7,
                        row=dict(uid=uid, title='server merged title', status='todo', version=7,
                                 created_at=1))

    receiver = Merged()
    receiver.status = 'merged'
    receiver.lose_reply = True
    with pytest.raises(OSError):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    op = json.loads(journal.pending_wire(live))
    assert op['table'] == 'tasks' and op['uid'] == tid
    change = sync.RemoteChange(seq=7, table='tasks', uid=tid, op='upsert',
                               fields={'title': 'server merged title'}, version=7,
                               hlc=op['hlc'], origin_gateway='macbook',
                               client_op_id=op['client_op_id'])
    if echo_before_settle:
        frozen = journal.pending_wire(live)
        sync.apply_remote_changes(live, [change], new_cursor=7, observers=False)
        assert journal.pending_wire(live) == frozen  # custody kept until settlement
        assert live.execute('SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()
    receiver.lose_reply = False
    result = client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert result.merged == 1 and len(receiver.sent) == 1  # no resend
    assert journal.pending_wire(live) is None
    if not echo_before_settle:
        sync.apply_remote_changes(live, [change], new_cursor=7, observers=False)
    assert sync.sync_state(live)['cursor'] == 7
    row = live.execute('SELECT title,sync_version FROM tasks WHERE id=?', (tid,)).fetchone()
    assert tuple(row) == ('server merged title', 7)
