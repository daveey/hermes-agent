"""Current-row recovery is not an invented historical replay result. [daveey]"""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


class CurrentRow(helpers.Receiver):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.evidence = None
        self.reads = 0

    def _call(self, method, path, body=None):
        result = super()._call(method, path, body)
        if path.endswith('/ops'):
            verdict = json.loads(self.slot['last_verdict'])
            verdict.pop('version')
            self.slot['last_verdict'] = json.dumps(verdict)
        return result

    def row_evidence(self, board, table, uid):
        assert not self.conn.in_transaction, 'network read must not reserve the writer'
        self.reads += 1
        return self.evidence


def issued_parent(conn):
    tid = kb.create_task(conn, title='synthetic original', assignee='worker')
    with kbc.write_txn(conn):
        conn.execute('DELETE FROM kanban_sync_outbox')
    kb.add_comment(conn, tid, 'author', 'dependent child')
    receiver = CurrentRow(conn)
    # Commit a versionless result, then lose inspection before local recording.
    original = receiver._call

    def lose_inspect(method, path, body=None):
        if path.endswith('/inspect') and receiver.sent:
            raise OSError('lost committed result')
        return original(method, path, body)

    receiver._call = lose_inspect
    with pytest.raises(OSError, match='lost committed result'):
        client.push(conn, receiver, 'default', 'macbook', primary=False, max_batches=1)
    receiver._call = original
    receiver.evidence = dict(board='default', table='tasks', uid=tid, state='live',
                             head=91, floor=0, feed_floor=0, tombstone=None,
                             row=dict(uid=tid, id=tid, title='current remote title',
                                      body='remote body', status='todo', version=19))
    return tid, receiver


@pytest.mark.parametrize('newer_pending', [False, True])
@pytest.mark.parametrize('status', ['applied', 'merged'])
def test_versionless_parent_recovers_current_row_after_reopen_without_resend(live, newer_pending, status):
    tid, receiver = issued_parent(live)
    retained = json.loads(receiver.slot['last_verdict'])
    retained['status'] = status
    receiver.slot['last_verdict'] = json.dumps(retained)
    frozen_verdict = receiver.slot['last_verdict']
    restarted = helpers.reopen(live)
    receiver.conn = restarted
    try:
        if newer_pending:
            with kbc.write_txn(restarted):
                restarted.execute('UPDATE tasks SET title=? WHERE id=?', ('new local title', tid))
        cursor = restarted.execute('SELECT cursor FROM kanban_sync_state').fetchone()[0]
        result = client.push(restarted, receiver, 'default', 'macbook', primary=False, max_batches=1)
        assert getattr(result, status) == 1 and result.sent == 0 and len(receiver.sent) == 1
        row = restarted.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        assert row['title'] == ('new local title' if newer_pending else 'current remote title')
        assert row['body'] == 'remote body' and row['sync_version'] == 19
        assert restarted.execute('SELECT cursor FROM kanban_sync_state').fetchone()[0] == cursor
        assert journal.snapshot(restarted)['last_verdict'] == frozen_verdict
        assert 'version' not in json.loads(frozen_verdict)
        assert journal.pending_wire(restarted) is None and receiver.slot['a'] == '1'
        assert not restarted.execute('SELECT 1 FROM kanban_sync_parent_envelopes').fetchone()
        assert restarted.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='task_comments'").fetchone()
        assert receiver.reads == 1
    finally:
        restarted.close()


@pytest.mark.parametrize('target,key,value', [
    ('evidence', 'board', 'foreign'), ('evidence', 'uid', 'foreign'),
    ('row', 'uid', 'foreign'), ('row', 'version', True),
    ('evidence', 'head', True), ('evidence', 'state', 'unknown'),
])
def test_unusable_current_row_keeps_exact_transport_and_child_custody(live, target, key, value):
    tid, receiver = issued_parent(live)
    targets = dict(evidence=receiver.evidence, row=receiver.evidence['row'])
    targets[target][key] = value
    frozen = journal.pending_wire(live)
    with pytest.raises(ValueError):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    assert journal.pending_wire(live) == frozen and journal.snapshot(live)['settled'] == '0'
    assert receiver.slot['a'] == '0' and len(receiver.sent) == 1
    assert live.execute('SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()
    assert live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='task_comments'").fetchone()


def test_projection_failure_rolls_back_image_verdict_and_parent_retirement(live, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    tid, receiver = issued_parent(live)
    frozen = journal.pending_wire(live)
    original = sync.apply_remote_changes

    def fail_after_projection(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('projection failed after savepoint release')

    monkeypatch.setattr(sync, 'apply_remote_changes', fail_after_projection)
    with pytest.raises(OSError, match='projection failed'):
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    row = live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    assert row['title'] == 'synthetic original' and row['sync_version'] is None
    assert journal.pending_wire(live) == frozen and journal.snapshot(live)['settled'] == '0'
    assert receiver.slot['a'] == '0' and len(receiver.sent) == 1
    assert live.execute('SELECT 1 FROM kanban_sync_parent_envelopes WHERE task_id=?', (tid,)).fetchone()
    monkeypatch.setattr(sync, 'apply_remote_changes', original)
    assert client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1).applied == 1
    assert len(receiver.sent) == 1


def test_local_tombstone_cannot_silently_skip_current_projection_and_release_parent(live):
    tid, receiver = issued_parent(live)
    with kbc.write_txn(live):
        live.execute("INSERT INTO kanban_sync_tombstones VALUES ('tasks',?,1)", (tid,))
    frozen = journal.pending_wire(live)
    try:
        client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1)
    except ValueError:
        pass
    assert journal.pending_wire(live) == frozen, 'skipped projection released unresolved parent'
    assert journal.snapshot(live)['settled'] == '0' and receiver.slot['a'] == '0'


def test_current_read_never_regresses_a_newer_pulled_image(live):
    from hermes_cli import kanban_db_sync as sync
    tid, receiver = issued_parent(live)
    # A later feed image won before the recovery read's older response arrived.
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_state SET applying=1')
        live.execute("UPDATE tasks SET title='newer feed', sync_version=30 WHERE id=?", (tid,))
        live.execute('UPDATE kanban_sync_state SET applying=0')
    assert client.push(live, receiver, 'default', 'macbook', primary=False, max_batches=1).applied == 1
    row = live.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    assert row['title'] == 'newer feed' and row['sync_version'] == 30
    assert journal.pending_wire(live) is None
    with kbc.write_txn(live):
        with pytest.raises(ValueError, match='observers=False'):
            sync.apply_remote_changes(live, [], new_cursor=0, allow_nested=True)
