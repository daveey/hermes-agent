"""Primary replay recovery preserves current projection and final transport custody."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_replay_journal as journal
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_replay_sender as helpers

live = helpers.live


class Primary(helpers.Receiver):
    def __init__(self, conn):
        super().__init__()
        self.conn = conn
        self.evidence = None
        self.reads = []
        self.status = 'merged'

    def row_evidence(self, board, table, uid):
        assert not self.conn.in_transaction
        self.reads.append((board, table, uid))
        return self.evidence


def lost_primary(conn):
    tid = kb.create_task(conn, title='primary original', assignee='worker')
    receiver = Primary(conn)
    receiver.lose_reply = True
    error = None
    try:
        client.push(conn, receiver, 'default', 'macbook', primary=True, max_batches=1)
    except (OSError, ValueError) as exc:
        error = exc
    assert isinstance(error, OSError), 'primary must issue before the controlled lost committed reply'
    assert len(receiver.sent) == 1 and journal.pending_wire(conn) is not None
    receiver.lose_reply = False
    receiver.evidence = dict(board='default', table='tasks', uid=tid, state='live', head=91,
                             row=dict(uid=tid, title='remote current', body='remote body', status='todo', version=19))
    return tid, receiver


@pytest.mark.parametrize('newer_pending', [False, True])
@pytest.mark.parametrize('status', ['applied', 'merged'])
def test_primary_merged_lost_reply_reopen_projects_once(live, monkeypatch, newer_pending, status):
    tid, receiver = lost_primary(live)
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict['status'] = status
    receiver.slot['last_verdict'] = json.dumps(verdict)
    retained = receiver.slot['last_verdict']
    reopened = helpers.reopen(live)
    receiver.conn = reopened
    observed = []
    original = sync._after_commit

    def observe(conn, result, board):
        assert not conn.in_transaction
        assert journal.pending_wire(conn) is None
        assert conn.execute('SELECT body FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'remote body'
        observed.append(result)
        original(conn, result, board)

    monkeypatch.setattr(sync, '_after_commit', observe)
    try:
        if newer_pending:
            with kbc.write_txn(reopened):
                reopened.execute("UPDATE tasks SET title='later local' WHERE id=?", (tid,))
        result = client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1)
        assert getattr(result, status) == 1 and result.sent == 0 and len(receiver.sent) == 1
        row = reopened.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        assert row['title'] == ('later local' if newer_pending else 'remote current')
        assert row['body'] == 'remote body' and row['sync_version'] == 19
        assert len(observed) == 1 and observed[0].changed_fields[tid] >= {'body'}
        assert journal.snapshot(reopened)['last_verdict'] == retained
        assert 'row' not in json.loads(retained)
        assert receiver.reads == [('default', 'tasks', tid)] and receiver.slot['a'] == '1'
        client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=0)
        assert len(observed) == 1 and len(receiver.sent) == 1
    finally:
        reopened.close()


def test_primary_success_acknowledges_omissions_before_pruning(live, monkeypatch):
    from hermes_cli import kanban_sync_omissions as omissions
    from hermes_cli import kanban_replica_lag as lag
    tid, receiver = lost_primary(live)
    op_id = receiver.sent[0]['client_op_id']
    receiver.evidence['row']['title'] = 'primary original'
    with kbc.write_txn(live):
        live.execute('INSERT INTO kanban_sync_omissions '
                     '(original_op_id,table_name,row_uid,field,compat_op_id) VALUES (?,?,?,?,?)',
                     (op_id, 'tasks', tid, 'title', op_id))
    reopened = helpers.reopen(live)
    receiver.conn = reopened
    acks, lagged = [], []
    acknowledge, acked = omissions.acknowledge, lag.acked

    def omission_ack(conn, identity, image, *, sent=None):
        assert conn.in_transaction and identity == op_id
        assert conn.execute('SELECT 1 FROM kanban_sync_outbox WHERE op_id=?', (op_id,)).fetchone()
        acks.append(identity)
        acknowledge(conn, identity, image, sent=sent)

    def lag_ack(path, tasks):
        assert not reopened.in_transaction and journal.pending_wire(reopened) is None
        lagged.append(set(tasks))
        acked(path, tasks)

    monkeypatch.setattr(omissions, 'acknowledge', omission_ack)
    monkeypatch.setattr(lag, 'acked', lag_ack)
    try:
        client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1)
        assert reopened.execute('SELECT state FROM kanban_sync_omissions').fetchone()[0] == 'delivered'
        assert acks == [op_id] and lagged == [{tid}]
        client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=0)
        assert acks == [op_id] and lagged == [{tid}] and len(receiver.sent) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize('reason,detail', [
    ('forbidden_field', 'not writable on tasks: body'),
    ('live_claim', 'another gateway holds the live lease'),
])
def test_primary_refusal_recovers_projection_and_visible_repair(live, monkeypatch, reason, detail):
    from hermes_cli import kanban_sync_compat as compat
    from hermes_cli import kanban_sync_live_claim as claims
    tid, receiver = lost_primary(live)
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict.update(status='rejected', reason=reason, detail=detail)
    receiver.slot['last_verdict'] = json.dumps(verdict)
    retained = receiver.slot['last_verdict']
    reopened = helpers.reopen(live)
    receiver.conn = reopened
    calls = []
    enqueue, refused = compat.enqueue_retry, claims.refused

    def repair(*args, **kwargs):
        assert journal.pending_wire(reopened) is None, 'repair cannot rewrite issued custody'
        calls.append('repair')
        return enqueue(*args, **kwargs)

    def claim(*args, **kwargs):
        calls.append('claim')
        return refused(*args, **kwargs)

    monkeypatch.setattr(compat, 'enqueue_retry', repair)
    monkeypatch.setattr(claims, 'refused', claim)
    try:
        result = client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1)
        assert result.rejected == 1 and result.applied == result.merged == 0
        row = reopened.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
        assert row['title'] == 'remote current' and row['sync_version'] == 19
        assert calls == (['repair'] if reason == 'forbidden_field' else ['claim'])
        assert reopened.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='sync_rejected'",
                                (tid,)).fetchone()[0] == 1
        if reason == 'forbidden_field':
            assert reopened.execute("SELECT 1 FROM kanban_sync_outbox WHERE op='compat'").fetchone()
            assert reopened.execute("SELECT 1 FROM kanban_sync_omissions WHERE field='body'").fetchone()
        assert journal.snapshot(reopened)['last_verdict'] == retained and receiver.slot['a'] == '1'
        client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=0)
        assert len(calls) == 1 and len(receiver.sent) == 1
    finally:
        reopened.close()


def test_primary_projection_failure_rolls_back_verdict_and_repair(live, monkeypatch):
    tid, receiver = lost_primary(live)
    frozen = journal.pending_wire(live)
    observed = []
    original = sync.apply_remote_changes

    def fail_after_projection(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('primary projection failure')

    monkeypatch.setattr(sync, 'apply_remote_changes', fail_after_projection)
    monkeypatch.setattr(sync, '_after_commit', lambda *args: observed.append(args))
    with pytest.raises(OSError, match='primary projection failure'):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert journal.pending_wire(live) == frozen and journal.snapshot(live)['settled'] == '0'
    assert live.execute('SELECT title FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'primary original'
    assert observed == [] and receiver.slot['a'] == '0' and len(receiver.sent) == 1
    monkeypatch.setattr(sync, 'apply_remote_changes', original)
    assert client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1).merged == 1
    assert len(observed) == 1 and len(receiver.sent) == 1


@pytest.mark.parametrize('local_version', [7, 30])
def test_primary_refused_child_restores_current_without_regressing_newer_pull(live, monkeypatch, local_version):
    from hermes_cli import kanban_sync_live_claim as claims
    tid = kb.create_task(live, title='parent', assignee='worker')
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_state SET applying=1')
        live.execute('UPDATE tasks SET sync_version=1 WHERE id=?', (tid,))
        live.execute('DELETE FROM kanban_sync_outbox')
        live.execute('UPDATE kanban_sync_state SET applying=0')
        rid = live.execute("INSERT INTO task_runs (task_id,profile,status,outcome,started_at,summary) "
                           "VALUES (?,'default','done','completed',2000,'refused handoff')", (tid,)).lastrowid
    uid = live.execute('SELECT uid FROM task_runs WHERE id=?', (rid,)).fetchone()[0]
    receiver = Primary(live)
    receiver.lose_reply = True
    with pytest.raises(OSError, match='reply lost'):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert receiver.sent[0]['table'] == 'task_runs'
    verdict = json.loads(receiver.slot['last_verdict'])
    verdict.update(status='rejected', reason='live_claim', detail='other lease')
    receiver.slot['last_verdict'] = json.dumps(verdict)
    receiver.lose_reply = False
    receiver.evidence = dict(board='default', table='task_runs', uid=uid, state='live', head=99,
                             row=dict(uid=uid, task_id=tid, status='running', outcome=None,
                                      started_at=2000000, summary=None, version=19))
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_state SET applying=1')
        live.execute('UPDATE task_runs SET sync_version=? WHERE uid=?', (local_version, uid))
        live.execute('UPDATE kanban_sync_state SET applying=0')
    reopened = helpers.reopen(live)
    receiver.conn = reopened
    restores = []
    restore = claims.restore_child

    def restore_once(*args, **kwargs):
        restores.append(uid)
        return restore(*args, **kwargs)

    monkeypatch.setattr(claims, 'restore_child', restore_once)
    try:
        assert client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=1).rejected == 1
        row = reopened.execute('SELECT * FROM task_runs WHERE uid=?', (uid,)).fetchone()
        assert row['sync_version'] == max(19, local_version)
        assert row['status'] == ('running' if local_version == 7 else 'done')
        assert restores == ([uid] if local_version == 7 else [])
        client.push(reopened, receiver, 'default', 'macbook', primary=True, max_batches=0)
        assert len(receiver.sent) == 1
        assert len(restores) == (1 if local_version == 7 else 0)
    finally:
        reopened.close()


@pytest.mark.parametrize('target,key,value', [
    ('evidence', 'board', 'foreign'), ('evidence', 'table', 'task_runs'),
    ('evidence', 'uid', 'foreign'), ('evidence', 'state', 'unknown'),
    ('evidence', 'head', True), ('evidence', 'head', -1),
    ('evidence', 'row', None), ('row', 'uid', 'foreign'),
    ('row', 'version', True), ('row', 'version', 0),
    ('row', 'deleted_at', 1),
])
def test_primary_unbound_current_evidence_does_not_release_transport(live, target, key, value):
    _tid, receiver = lost_primary(live)
    frozen = journal.pending_wire(live)
    selected = receiver.evidence if target == 'evidence' else receiver.evidence['row']
    selected[key] = value
    with pytest.raises(ValueError, match='primary replay live current row unavailable'):
        client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1)
    assert journal.pending_wire(live) == frozen and journal.snapshot(live)['settled'] == '0'
    assert receiver.slot['a'] == '0' and len(receiver.sent) == 1


def test_primary_current_task_projection_preserves_newer_pulled_version(live):
    tid, receiver = lost_primary(live)
    with kbc.write_txn(live):
        live.execute('UPDATE kanban_sync_state SET applying=1')
        live.execute("UPDATE tasks SET title='newer feed',sync_version=30 WHERE id=?", (tid,))
        live.execute('UPDATE kanban_sync_state SET applying=0')
    assert client.push(live, receiver, 'default', 'macbook', primary=True, max_batches=1).merged == 1
    row = live.execute('SELECT title,sync_version FROM tasks WHERE id=?', (tid,)).fetchone()
    assert tuple(row) == ('newer feed', 30)
    assert journal.pending_wire(live) is None and len(receiver.sent) == 1
