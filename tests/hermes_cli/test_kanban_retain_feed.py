"""A confirmed own-run retain may clear its live local claim; an ACK may not."""
import json
import os

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_release_claim as claims
from tests.hermes_cli import test_kanban_retain_receipt as support

live = support.live


@pytest.mark.parametrize('confirmed', [True, False])
def test_feed_retained_claim_release_requires_exact_confirmed_command(live, confirmed):
    owned, receipt, _, _hub = support.fixture(live)
    lock = kb._host_prefix() + ':1234'
    with kbc.write_txn(live):
        live.execute('UPDATE tasks SET claim_lock=?, worker_pid=?,worker_started_at=? WHERE id=?',
                     (lock, os.getpid(), 'fixture-birth', owned['task_id']))
        old = live.execute('SELECT * FROM tasks WHERE id=?', (owned['task_id'],)).fetchone()
        live.execute("UPDATE tasks SET status='triage' WHERE id=?", (owned['task_id'],))
        if confirmed:
            live.execute("UPDATE kanban_failure_commands SET delivery_state='confirmed',canonical_receipt_json=? WHERE run_uid=?",
                         (json.dumps(receipt), owned['run_uid']))
        assert claims.release_on_feed(live, owned['task_id'], old, 'triage') is confirmed
    row = kb.get_task(live, owned['task_id'])
    assert (row.claim_lock is None) is confirmed


@pytest.mark.parametrize('feed_before_confirmation', [False, True])
def test_confirmed_projection_settles_ended_run_and_live_claim_atomically(live, feed_before_confirmation):
    from hermes_cli import kanban_retain_transport as transport
    from hermes_cli import kanban_db_sync as sync
    owned, receipt, rows, hub = support.fixture(live)
    tid, uid = owned['task_id'], owned['run_uid']
    with kbc.write_txn(live):
        live.execute('UPDATE tasks SET claim_lock=?,worker_pid=?,worker_started_at=? WHERE id=?',
                     (kb._host_prefix() + ':1234', os.getpid(), 'fixture-birth', tid))
    for (table, ident), value in rows.items():
        value.update(uid=ident, version=10, hlc='9999999999999-0000-server', origin_gateway=hub.ident.gateway)
        if table == 'task_events':
            value.update(created_at=123, ord=42)
    rows['task_events', owned['event_uid']]['version'] = 1
    rows['tasks', tid].update(id=tid, title='Retained fixture', assignee='worker', lease_holder=None, lease_expires=None)
    rows['task_events', receipt['episode_uid']]['payload'] = json.dumps({'accounting_boundary':False})
    with kbc.write_txn(live):
        live.execute('UPDATE task_runs SET claim_lock=? WHERE uid=?', (kb._host_prefix() + ':1234', uid))
    if feed_before_confirmation:
        from hermes_cli.kanban_sync_client import _row_change
        sync.apply_remote_changes(live, [_row_change('tasks', rows['tasks', tid])], new_cursor=1, board='default')
    result = transport.confirm(live, hub, 'default', uid)
    cursor = sync.sync_state(live)['cursor']
    transport.project(live, hub, 'default', result)
    task = kb.get_task(live, tid)
    assert task.status == 'triage' and task.current_run_id is None and task.claim_lock is None
    assert live.execute('SELECT outcome FROM task_runs WHERE uid=?', (uid,)).fetchone()[0] == 'retained'
    assert sync.sync_state(live)['cursor'] == cursor
    assert not sync.outbox(live)
    from hermes_cli import kanban_spend_decision as decision
    assert decision.execution_restricted(live, tid)
    with kbc.write_txn(live):
        kb._append_event(live, tid, 'promoted_manual', {})
    assert decision.pending(live, tid)
