"""Prior-home evidence is not replayed by a cursor-forward primary partition."""
from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_pickup_history as history
from hermes_cli import kanban_spend_decision as spend
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_worker_node as node
from tests.hermes_cli import test_kanban_worker_node as fixtures

gws, svc = fixtures.gws, fixtures.svc


def _world(gws):
    hub, worker = gws
    worker.enter()
    tid = kb.create_task(worker.conn, title="prior home stop", assignee="default")
    with worker.conn:
        run = worker.conn.execute(
            "INSERT INTO task_runs(task_id,profile,status,started_at,ended_at,outcome) "
            "VALUES (?,'default','done',100,110,'retained')", (tid,)).lastrowid
        kb._append_event(worker.conn, tid, 'spend_budget_triaged',
                         {'cause': 'capability_unavailable'}, run_id=run)
        episode = worker.conn.execute(
            "SELECT uid FROM task_events WHERE task_id=? AND kind='spend_budget_triaged'", (tid,)).fetchone()[0]
        kb._append_event(worker.conn, tid, 'decision_retained', {'episode': episode}, run_id=run)
    worker.round()
    hub.round()  # cursor passes the foreign rows; they survive only in the replica
    assert kb.get_task(hub.conn, tid) is None
    hub.enter()
    replica = node.open_replica(hub.conn)
    assert replica is not None
    card = replica.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    fields = client._row_fields(replica, 'tasks', card)
    fields['created_on'] = 'hub'
    changes = [sync.RemoteChange(0, 'tasks', tid, 'upsert', fields, card['sync_version'] + 1, '', 'server')]
    # A later event references the old run, producing the real incident's pending_sync placeholder.
    run_uid = replica.execute('SELECT uid FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
    changes += [sync.RemoteChange(0, 'task_events', sync.uuid7(), 'upsert',
                                 {'task_id': tid, 'kind': 'worker_result', 'run_uid': run_uid,
                                  'created_at': 200000}, 0, '', 'worker-1')]
    rehome = sync.RemoteChange(0, 'task_events', sync.uuid7(), 'upsert',
                              {'task_id': tid, 'kind': 'rehomed', 'payload': json.dumps(
                                  {'from': 'worker-1', 'to': 'hub'}), 'created_at': 300000}, 0, '', 'server')
    changes.append(rehome)
    replica.close()
    # Simulate Board-authenticated feed application to both stores, without publishing fixture edits.
    raw = client.open_shadow(fixtures.BOARD, 'hub')
    sync.apply_remote_changes(raw, changes, new_cursor=sync.sync_state(raw)['cursor'], observers=False)
    raw.close()
    sync.apply_remote_changes(hub.conn, changes, new_cursor=sync.sync_state(hub.conn)['cursor'], observers=False)
    return hub, tid, episode, run_uid


def test_supported_refill_recovers_own_middle_gap_and_placeholder(gws):
    hub, tid, episode, run_uid = _world(gws)
    conn = hub.conn
    assert spend.pending_episode(conn, tid) is None
    placeholder = conn.execute('SELECT id,status FROM task_runs WHERE uid=?', (run_uid,)).fetchone()
    assert placeholder['status'] == 'pending_sync'
    before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    outbox = [tuple(r) for r in conn.execute('SELECT * FROM kanban_sync_outbox')]
    top = conn.execute('SELECT MAX(id) FROM task_events').fetchone()[0]
    replica = node.open_replica(conn)
    try:
        report = history.refill(conn, replica, 'hub', [tid])
        assert report[tid]['filled'] >= 3, report
        assert report[tid]['unresolved'] == 0
        stop = spend.pending_episode(conn, tid)
        assert stop['uid'] == episode
        assert conn.execute('SELECT status FROM task_runs WHERE id=?', (placeholder['id'],)).fetchone()[0] == 'done'
        assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE uid=?', (run_uid,)).fetchone()[0] == 1
        assert dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()) == before
        assert [tuple(r) for r in conn.execute('SELECT * FROM kanban_sync_outbox')] == outbox
        assert conn.execute('SELECT MAX(id) FROM task_events').fetchone()[0] == top
        assert history.refill(conn, replica, 'hub', [tid])[tid]['filled'] == 0
        restored = conn.execute('SELECT origin_gateway FROM task_events WHERE uid=?', (episode,)).fetchone()[0]
        original = replica.execute('SELECT origin_gateway FROM task_events WHERE uid=?', (episode,)).fetchone()[0]
        assert restored == original
        with conn:
            kb._append_event(conn, tid, 'promoted_manual', {'reason': 'owner decision'})
        assert spend.pending_episode(conn, tid) is None
    finally:
        replica.close()


@pytest.mark.parametrize('veto', ['no-rehome', 'wrong-home', 'tombstone', 'peer-marker', 'payload-mismatch'])
def test_recovery_requires_matching_home_and_preserves_tombstones(gws, veto):
    hub, tid, episode, _ = _world(gws)
    conn = hub.conn
    with conn:
        conn.execute('UPDATE kanban_sync_state SET applying=1')
        sql, args = {
            'no-rehome': ("DELETE FROM task_events WHERE task_id=? AND kind='rehomed'", (tid,)),
            'wrong-home': ("UPDATE task_events SET payload=? WHERE task_id=? AND kind='rehomed'",
                           (json.dumps({'from': 'worker-1', 'to': 'another-home'}), tid)),
            'peer-marker': ("UPDATE task_events SET origin_gateway='worker-1' WHERE task_id=? AND kind='rehomed'", (tid,)),
            'payload-mismatch': ("UPDATE task_events SET payload=? WHERE task_id=? AND kind='rehomed'",
                                 (json.dumps({'from': 'another-prior-home', 'to': 'hub'}), tid)),
            'tombstone': ('INSERT INTO kanban_sync_tombstones(table_name,row_uid,deleted_at) VALUES (?,?,?)',
                          ('task_events', episode, 400000)),
        }[veto]
        conn.execute(sql, args)
        conn.execute('UPDATE kanban_sync_state SET applying=0')
    replica = node.open_replica(conn)
    try:
        history.refill(conn, replica, 'hub', [tid])
        assert conn.execute('SELECT 1 FROM task_events WHERE uid=?', (episode,)).fetchone() is None
    finally:
        replica.close()


def test_cli_and_normal_content_repair_recover_prior_home(gws, capsys):
    from hermes_cli import kanban_sync_cli as cli
    from hermes_cli import kanban_sync_repair_content as content

    hub, tid, episode, _ = _world(gws)
    args = argparse.Namespace(board=fixtures.BOARD, task_ids=[tid], json=True)
    assert cli._refill_history(args) == 0
    assert json.loads(capsys.readouterr().out)[tid]['filled'] >= 3
    assert spend.pending_episode(hub.conn, tid)['uid'] == episode
    with hub.conn:
        hub.conn.execute('UPDATE kanban_sync_state SET applying=1')
        hub.conn.execute('DELETE FROM task_events WHERE uid=?', (episode,))
        hub.conn.execute('UPDATE kanban_sync_state SET applying=0')
    replica = node.open_replica(hub.conn)
    assert replica is not None
    try:
        content.run(hub.conn, replica, 'hub', path=hub.home / 'kanban.db', inputs=None)
        assert spend.pending_episode(hub.conn, tid)['uid'] == episode
    finally:
        replica.close()


@pytest.mark.parametrize('run_state', ['missing', 'real-local', 'pending-write'])
def test_real_runs_and_unresolved_run_evidence_are_not_overwritten(gws, run_state):
    hub, tid, episode, run_uid = _world(gws)
    conn = hub.conn
    if run_state == 'missing':
        raw = client.open_shadow(fixtures.BOARD, 'hub')
        with raw:
            raw.execute('UPDATE kanban_sync_state SET applying=1')
            raw.execute("UPDATE task_runs SET status='pending_sync' WHERE uid=?", (run_uid,))
            raw.execute('UPDATE kanban_sync_state SET applying=0')
        raw.close()
    else:
        with conn:
            if run_state == 'real-local':
                conn.execute("UPDATE task_runs SET status='running',summary='preserve local' WHERE uid=?", (run_uid,))
            else:
                conn.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                             "VALUES (?,'task_runs',?,'upsert','{}','',1)", (sync.uuid7(), run_uid))
    replica = node.open_replica(conn)
    assert replica is not None
    try:
        result = history.refill(conn, replica, 'hub', [tid])[tid]
        if run_state == 'real-local':
            assert spend.pending_episode(conn, tid)['uid'] == episode
            assert conn.execute('SELECT summary FROM task_runs WHERE uid=?', (run_uid,)).fetchone()[0] == 'preserve local'
        else:
            assert result['unresolved'] == 2
            assert spend.pending_episode(conn, tid) is None
    finally:
        replica.close()
