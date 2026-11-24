"""Verdict-free own-run end after a definitive inapplicable retain. [daveey]

Never turn unknown delivery into a new intent or fabricate a decision episode.
The ordinary released outcome is not unused_grant: actual spend remains counted.
"""
from __future__ import annotations

import hashlib
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_failure_release as release
from hermes_cli import kanban_retain_transport as transport


def _eligible(conn, actor, data):
    owned = transport.command(conn, actor.run_uid)
    if (not owned or owned['task_id'] != actor.task or owned['operation_kind'] != 'retain'
            or owned['delivery_state'] != 'rejected'):
        return None
    wire = json.loads(owned['request_json'])
    if (hashlib.sha256(transport.canonical(wire).encode()).hexdigest() != owned['request_sha256']
            or wire.get('client_op_id') != owned['op_id'] or wire.get('task') != actor.task
            or wire.get('run_uid') != actor.run_uid or wire.get('event_uid') != owned['event_uid']
            or wire.get('payload') != data or wire.get('expected_episode') is not None
            or wire.get('payload', {}).get('reason') != 'decision_pending'):
        return None
    rejected = conn.execute("SELECT last_error FROM kanban_sync_outbox WHERE op_id=? AND op='retain' "
                            "AND state='rejected'", (owned['op_id'],)).fetchone()
    if not rejected or rejected[0] != 'missing_decision_episode':
        return None
    return owned


def _keep(conn, change, actor):
    """Runs are immutable identities; task evidence must not erase a newer claim.

    Called by apply_remote_changes inside its writer transaction, not at the
    earlier network read. Pending local lifecycle fields retain normal priority.
    """
    if change.table != 'tasks':
        return True
    row = conn.execute('SELECT t.lease_version,r.uid FROM tasks t LEFT JOIN task_runs r '
                       'ON r.id=t.current_run_id WHERE t.id=?', (actor.task,)).fetchone()
    return row is None or (int(row['lease_version'] or 0) <= change.version
                           and row['uid'] in (None, actor.run_uid))


def recover(conn, hub, board, actor, data):
    """Return None for all other failures; caller preserves its original exception."""
    owned = _eligible(conn, actor, data)
    if owned is None:
        return None
    from hermes_cli import kanban_containment_actor as actors
    from hermes_cli import kanban_sync_client as client
    with kb.write_txn(conn):
        # A previous observer's fallback is never replaced or reclassified.
        if not owned['rejected_release_op_id']:
            actors.current_actor(conn)
            run = conn.execute('SELECT id FROM task_runs WHERE uid=? AND task_id=?',
                               (actor.run_uid, actor.task)).fetchone()
            release.queue(conn, actor.task, run['id'], outcome='released',
                          error='retain rejected: missing_decision_episode')
        owned = transport.command(conn, actor.run_uid)
        out = conn.execute("SELECT fields FROM kanban_sync_outbox WHERE op_id=? AND op='release'",
                           (owned['rejected_release_op_id'],)).fetchone()
        if out and json.loads(out[0]).get('outcome') != 'released':
            raise ValueError('retain rejected: existing failure recovery retains custody')
    client.push(conn, hub, board, hub.ident.gateway, primary=True)
    run = transport._evidence(hub, board, 'task_runs', actor.run_uid)
    task = transport._evidence(hub, board, 'tasks', actor.task)
    metadata = json.loads(run.get('metadata') or '{}')
    if (not isinstance(metadata, dict) or task.get('uid') != actor.task
            or run.get('task_id') != actor.task or run.get('uid') != actor.run_uid
            or run.get('ended_at') is None or run.get('outcome') != 'released'
            or metadata.get('unused_grant') or task.get('current_run_uid') == actor.run_uid):
        raise ValueError('retain rejected: fallback release not independently confirmed; preserve custody')
    changes = [client._row_change('task_runs', run), client._row_change('tasks', task)]
    sync.apply_remote_changes(conn, changes, new_cursor=sync.sync_state(conn)['cursor'], board=board,
                              local_pending_wins=True, keep=lambda db, change: _keep(db, change, actor))
    # The admitted actor voluntarily ended this exact claim; do not clear a successor.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET claim_lock=NULL,claim_expires=NULL,worker_pid=NULL,worker_started_at=NULL "
                     "WHERE id=? AND current_run_id IS NULL AND claim_lock="
                     "(SELECT claim_lock FROM task_runs WHERE uid=?)", (actor.task, actor.run_uid))
    return {'ok': True, 'task_id': actor.task, 'run_uid': actor.run_uid,
            'request_uid': owned['request_uid'], 'release_op_id': owned['rejected_release_op_id'],
            'terminal_for_run': True, 'dependency_satisfied': False, 'retained': False,
            'status': task['status'], 'disposition': 'retain_rejected_run_released',
            'next_step': 'Retain was rejected, not applied. Existing dependency, scope and spend admission still apply.'}
