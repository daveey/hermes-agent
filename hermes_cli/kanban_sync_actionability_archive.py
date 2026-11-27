"""Read-only proof that canonical archive withdrew a rejected diagnostic. [daveey]

Withdrawal is not completion, delivery, authorization or transport retirement.
The caller has already verified immutable rejected-command custody.
"""
from __future__ import annotations

import json

from hermes_cli.kanban_resume_exec import _LIFECYCLE_KINDS
from hermes_cli.kanban_sync_actionability_terminal import _before, _event, _IDLE, _TRANSITIONS


_PHASES = frozenset(('todo', 'ready', 'running', 'review', 'blocked', 'scheduled', 'triage', 'done'))
_RUN_KEYS = ('uid', 'status', 'outcome', 'started_at', 'ended_at')


def _history(events, archive):
    """Validate stamped predecessor references without treating identities as clocks."""
    lifecycle = [e for e in events if e['kind'] in _LIFECYCLE_KINDS]
    identities = {}
    for event in lifecycle:
        data = event['payload'].get('_recovery_history_v1')
        if '_recovery_history_v1' in event['payload'] and (not isinstance(data, dict)
                or not isinstance(data.get('identity'), str) or not data['identity']
                or not isinstance(data.get('predecessors'), list)
                or any(not isinstance(p, str) or not p for p in data['predecessors'])
                or len(set(data['predecessors'])) != len(data['predecessors'])):
            return False
        key = data['identity'] if data else event['uid']
        if key in identities:
            return False
        identities[key] = event
    if not archive['payload'].get('_recovery_history_v1'):
        return False
    observed = set()
    for event in lifecycle:
        for key in event['payload'].get('_recovery_history_v1', {}).get('predecessors', []):
            if key not in identities or not _before(identities[key], event):
                return False  # Also excludes cycles and self-references.
            observed.add(key)
    heads = [event for key, event in identities.items() if key not in observed]
    return heads == [archive]


def _runs(conn, task_id, archive):
    rows = [dict(r) for r in conn.execute('SELECT * FROM task_runs WHERE task_id=?', (task_id,))]
    portable = {}
    for run in rows:
        if (not isinstance(run['uid'], str) or not run['uid'] or run.get('deleted_at') is not None
                or type(run['started_at']) is not int or type(run['ended_at']) is not int
                or not 0 <= run['started_at'] <= run['ended_at'] <= archive['created_at']
                or not isinstance(run['status'], str) or run['status'] in ('', 'running')
                or not isinstance(run['outcome'], str) or not run['outcome']
                or run['uid'] in portable):
            return None
        portable[run['uid']] = {k: run[k] for k in _RUN_KEYS}
    attached = [r for r in rows if r['id'] == archive['run_id']]
    if archive['run_id'] is not None and len(attached) != 1:
        return None
    return {'all': portable, 'archive_run': attached[0]['uid'] if attached else None}


def _proof(conn, task_id, source_uid):
    rows = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchall()
    if len(rows) != 1:
        return None
    task = dict(rows[0])
    if (task['status'] != 'archived' or task.get('deleted_at') is not None
            or any(task[k] is not None for k in _IDLE)):
        return None
    raw = [dict(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=?', (task_id,))]
    sources = [e for e in raw if e['uid'] == source_uid]
    archives = [e for e in raw if e['kind'] == 'archived']
    if len(sources) != 1 or not archives:
        return None
    last = next((e for e in archives if all(e is other or _before(other, e) for other in archives)), None)
    if last is None or not _before(sources[0], last):
        return None
    original, archive = _event(sources[0]), _event(last)
    if archive['payload'].get('source_status') not in _PHASES:
        return None
    history = []
    for event in raw:
        try:
            data = json.loads(event['payload'])
        except (ValueError, TypeError):
            data = None
        if (event['kind'] in _TRANSITIONS or event['kind'] in _LIFECYCLE_KINDS
                or isinstance(data, dict) and 'status' in data):
            parsed = _event(event)
            if event is not last and not _before(event, last):
                return None
            history.append(parsed)
    if not _history(history, archive):
        return None
    runs = _runs(conn, task_id, last)
    if runs is None:
        return None
    return {'source': original, 'archive': archive, 'runs': runs, 'assignee': task['assignee'],
            'completed_at': task['completed_at'], 'history': sorted(history, key=lambda e: e['uid'])}


def superseded(live, shadow, task_id, source_uid):
    """Both copies must agree on archival provenance, causal history and closed runs."""
    current = _proof(live, task_id, source_uid)
    return current is not None and current == _proof(shadow, task_id, source_uid)
