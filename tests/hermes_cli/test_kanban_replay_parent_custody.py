"""Parent cache cleanup cannot erase outstanding strict transport custody."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client, kanban_sync_envelope as envelopes
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


def issued_parent(conn):
    tid = kb.create_task(conn, title='issued parent cache', assignee='worker')
    with kbc.write_txn(conn):
        conn.execute('DELETE FROM kanban_sync_outbox')
    kb.add_comment(conn, tid, 'author', 'dependent child')
    receiver = helpers.Receiver()
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(conn, receiver, 'default', 'macbook', primary=False, max_batches=1)
    return tid, receiver, journal.pending_wire(conn)


@pytest.mark.parametrize('change', ['delete', 'version'])
def test_prune_preserves_issued_parent_after_local_change(live, change):
    tid, receiver, frozen = issued_parent(live)
    saved = live.execute('SELECT wire FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()[0]
    with kbc.write_txn(live):
        if change == 'delete':
            live.execute('DELETE FROM tasks WHERE id=?', (tid,))
        else:
            live.execute('UPDATE tasks SET sync_version=9 WHERE id=?', (tid,))
        # An unrelated stale cache is still collectable.
        live.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire) VALUES ('gone-other','{}')")
        envelopes.prune_parents(live)
    assert live.execute('SELECT wire FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()[0] == saved
    assert not live.execute("SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id='gone-other'").fetchone()
    assert journal.pending_wire(live) == frozen
    restarted = helpers.reopen(live)
    try:
        receiver.lose_reply = False
        result = client.push(restarted, receiver, 'default', 'macbook', primary=False, max_batches=1)
        assert result.applied == 1 and len(receiver.sent) == 1
        assert journal.pending_wire(restarted) is None and receiver.slot['a'] == '1'
        assert not restarted.execute('SELECT 1 FROM kanban_sync_parent_envelopes').fetchone()
    finally:
        restarted.close()


def test_direct_retirement_requires_exact_final_verdict(live):
    tid, receiver, frozen = issued_parent(live)
    with pytest.raises(ValueError, match='issued parent requires final verdict'):
        with kbc.write_txn(live):
            envelopes.retire_parent(live, tid)
    assert journal.pending_wire(live) == frozen
    assert live.execute('SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()
    receiver.lose_reply = False
    assert client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1).applied == 1
    assert journal.pending_wire(live) is None


@pytest.mark.parametrize('damage', ['unconsumed', 'identity', 'binding'])
def test_retirement_revalidates_final_slot(live, damage):
    tid, receiver, frozen = issued_parent(live)
    remote = dict(receiver.slot)
    if damage == 'unconsumed':
        remote.update(h='0', last_op_id=None, last_payload_sha256=None, last_verdict=None)
    elif damage == 'identity':
        remote['last_op_id'] = 'other-operation'
        verdict = json.loads(remote['last_verdict'])
        verdict['client_op_id'] = remote['last_op_id']
        remote['last_verdict'] = json.dumps(verdict)
    else:
        remote['board_incarnation'] = 'other-board'
    with pytest.raises(ValueError):
        with kbc.write_txn(live):
            envelopes.retire_parent(live, tid, remote=remote)
    assert journal.pending_wire(live) == frozen
    assert live.execute('SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()


def test_bound_cleanup_requires_transaction_but_can_retire_unrelated_cache(live):
    tid, _, frozen = issued_parent(live)
    with pytest.raises(ValueError, match='caller transaction required'):
        envelopes.prune_parents(live)
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire) VALUES ('gone-other','{}')")
        envelopes.retire_parent(live, 'gone-other')
    assert not live.execute("SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id='gone-other'").fetchone()
    assert live.execute('SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()
    assert journal.pending_wire(live) == frozen


@pytest.mark.parametrize('pending', [False, True])
def test_idle_or_non_task_issuance_does_not_hold_unrelated_cache(live, pending):
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_sync_parent_envelopes(task_id,wire) VALUES ('gone-other','{}')")
        if pending:
            journal.issue(live, dict(client_op_id='comment-issued', table='task_comments', uid='gone-other', fields={}))
        envelopes.prune_parents(live)
    assert not live.execute('SELECT 1 FROM kanban_sync_parent_envelopes').fetchone()
    assert (journal.pending_wire(live) is not None) == pending


@pytest.mark.parametrize('damage', ['missing', 'changed'])
def test_exact_final_slot_does_not_authorize_changed_parent_cache(live, damage):
    tid, receiver, frozen = issued_parent(live)
    with kbc.write_txn(live):
        if damage == 'missing':
            live.execute('DELETE FROM kanban_sync_parent_envelopes')
        else:
            live.execute("UPDATE kanban_sync_parent_envelopes SET wire='{}'")
    with pytest.raises(ValueError, match='issued parent cache custody mismatch'):
        with kbc.write_txn(live):
            envelopes.retire_parent(live, tid, remote=receiver.slot)
    assert journal.pending_wire(live) == frozen


@pytest.mark.parametrize('damage', ['anchor', 'wire'])
def test_cleanup_refuses_corrupt_bound_journal_without_deleting_cache(live, damage):
    tid, _, _ = issued_parent(live)
    saved = live.execute('SELECT wire FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()[0]
    with kbc.write_txn(live):
        if damage == 'anchor':
            live.execute("UPDATE kanban_sync_state SET journal_id='different'")
        else:
            live.execute("UPDATE kanban_replay_sender1 SET wire=X'7b7d'")
    with pytest.raises(ValueError):
        with kbc.write_txn(live):
            envelopes.prune_parents(live)
    assert live.execute('SELECT wire FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()[0] == saved
