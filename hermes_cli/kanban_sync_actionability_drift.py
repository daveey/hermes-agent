"""Read-only audit of superseded, definitively rejected diagnostics. [daveey]

This is neither a delivery receipt nor permission to retire or replay transport.
"""
from __future__ import annotations

import sqlite3

from hermes_cli import kanban_actionability_transport as transport


def _valid_facts(facts, *, portable_only=False):
    if not isinstance(facts, dict) or set(facts) != {
            'status', 'assignee', 'current_run_uid', 'root_episode', 'source_uids', 'dependencies'}:
        return False
    if not isinstance(facts['status'], str) or any(
            value is not None and not isinstance(value, str)
            for value in (facts['assignee'], facts['current_run_uid'], facts['root_episode'])):
        return False
    sources, deps = facts['source_uids'], facts['dependencies']
    return (isinstance(sources, list) and len(sources) == 1
            and isinstance(sources[0], dict)
            and set(sources[0]) == ({'uid'} if portable_only else {'uid', 'version'})
            and isinstance(sources[0]['uid'], str) and bool(sources[0]['uid'])
            and (portable_only or type(sources[0]['version']) is int and sources[0]['version'] > 0)
            and isinstance(deps, list) and len(deps) <= 128
            and all(isinstance(d, dict) and set(d) == {'id', 'status'}
                    and isinstance(d['id'], str) and bool(d['id'])
                    and isinstance(d['status'], str) for d in deps)
            and [d['id'] for d in deps] == sorted({d['id'] for d in deps}))


def _source(conn, task_id):
    from hermes_cli.kanban_actionability_selection import select

    row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        return None
    task = dict(row)
    if task.get('deleted_at') is not None:
        return None
    run_uid = None
    if task['current_run_id'] is not None:
        run = conn.execute('SELECT uid FROM task_runs WHERE id=? AND task_id=?',
                           (task['current_run_id'], task_id)).fetchone()
        if run is None or not run[0]:
            return None
        run_uid = run[0]
    events = [dict(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=?', (task_id,))]
    # SQLite event rows carry no canonical version column. Compare portable
    # source identity, never invent a version or treat absent metadata as change.
    if any(not e.get('uid') or e.get('deleted_at') is not None for e in events):
        return None
    event = select(task['status'], events, episode=task['containment_episode'],
                   order_key=lambda e: (e['created_at'], e['id']))
    if event is None:
        return None
    deps = []
    for (pid,) in conn.execute('SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id', (task_id,)):
        parent = conn.execute('SELECT status,containment_episode FROM tasks WHERE id=?', (pid,)).fetchone()
        if parent is None:
            return None
        status = 'triage' if parent[0] in ('done', 'archived') and parent[1] else parent[0]
        deps.append({'id': pid, 'status': status})
    return {'status': task['status'], 'assignee': task['assignee'], 'current_run_uid': run_uid,
            'root_episode': task['containment_episode'],
            'source_uids': [{'uid': event['uid']}], 'dependencies': deps}


def audited_rejection(live, shadow, op):
    """Require intact rejected command custody and agreeing, changed source facts."""
    if (op.get('op') != transport.KIND or op.get('table_name') != 'tasks'
            or op.get('state') != 'rejected' or op.get('last_error') != 'actionability_source_changed'):
        return False
    try:
        wire = transport.frozen_wire(op)
        owned = transport.command(live, op['op_id'])
        text = transport.canonical(wire)
        if (not owned or owned.get('delivery_state') != 'rejected'
                or owned.get('canonical_receipt_json') is not None
                or owned.get('op_id') != wire['client_op_id'] or owned.get('task_id') != wire['task']
                or owned.get('contract_key') != wire['payload']['contract_key']
                or owned.get('phase') != wire['phase'] or owned.get('request_json') != text
                or owned.get('request_sha256') != transport.digest(text)
                or owned.get('operation_kind', transport.KIND) != transport.KIND
                or owned.get('request_uid', op['op_id']) != op['op_id']
                or transport.envelope(wire['task'], wire['payload'], wire['phase']) != wire):
            return False
        frozen = wire['payload']['facts']
        if not _valid_facts(frozen):
            return False
        comparable = {**frozen, 'source_uids': [{'uid': s['uid']} for s in frozen['source_uids']]}
        current, replica = _source(live, wire['task']), _source(shadow, wire['task'])
        return _valid_facts(current, portable_only=True) and current == replica and current != comparable
    except (ValueError, TypeError, KeyError, sqlite3.Error):
        return False  # Unknown schema, malformed custody or missing source stays dirty.
