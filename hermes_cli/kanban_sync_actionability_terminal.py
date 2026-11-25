"""Read-only, done-only proof for rejected diagnostic supersession. [daveey]

The shadow is the authoritative replica supplied by sync diff. This is not a
completion writer, an actionability source, or a transport retirement receipt.
"""
from __future__ import annotations

import json

from hermes_cli.kanban_actionability_selection import KINDS
from hermes_cli.kanban_sync_divergence import consistent_uid_milliseconds

# These could contradict completion, even when a stale row still says done.
_TRANSITIONS = frozenset(kind for kinds in KINDS.values() for kind in kinds) | {
    'completed', 'archived', 'reopened', 'review_reopened', 'status', 'status_healed',
    'spawned', 'worker_bound', 'gave_up', 'triage_swept', 'decision_retained',
    'contained', 'containment_resumed', 'claimed', 'changes_requested',
    'containment_released', 'containment_closed',
}
_IDLE = ('current_run_id', 'claim_lock', 'claim_expires', 'worker_pid', 'containment_episode')


def _clock(event):
    second = event['created_at']
    if type(second) is not int or second < 0:
        raise ValueError('invalid event second')
    return second, consistent_uid_milliseconds(event['uid'], second)


def _before(left, right):
    a, b = _clock(left), _clock(right)
    return a[0] < b[0] or (a[0] == b[0] and a[1] is not None and b[1] is not None and a[1] < b[1])


def _event(event):
    if (not isinstance(event['uid'], str) or not event['uid']
            or event.get('deleted_at') is not None):
        raise ValueError('missing event identity')
    _clock(event)
    payload = json.loads(event['payload'])
    if not isinstance(payload, dict):
        raise ValueError('invalid event payload')
    return {k: event[k] for k in ('uid', 'task_id', 'kind', 'created_at')} | {'payload': payload}


def _proof(conn, task_id, source_uid):
    rows = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchall()
    if len(rows) != 1:
        return None
    task = dict(rows[0])
    if (task['status'] != 'done' or task.get('deleted_at') is not None
            or any(task[k] is not None for k in _IDLE)
            or type(task['completed_at']) is not int):
        return None
    events = [dict(r) for r in conn.execute('SELECT * FROM task_events WHERE task_id=?', (task_id,))]
    sources = [e for e in events if e['uid'] == source_uid]
    completions = [e for e in events if e['kind'] == 'completed']
    if len(sources) != 1 or not completions:
        return None
    # A latest completion must be strictly ordered after every earlier completion.
    last = next((e for e in completions if all(
        other is e or _before(other, e) for other in completions)), None)
    if last is None or not _before(sources[0], last):
        return None
    original, completion = _event(sources[0]), _event(last)
    for event in events:
        try:
            data = json.loads(event['payload'])
        except (ValueError, TypeError):
            data = None
        transition = event['kind'] in _TRANSITIONS or isinstance(data, dict) and 'status' in data
        if transition and event is not last:
            _event(event)  # Malformed potential lifecycle evidence cannot disappear.
            if not _before(event, last):
                return None
    # Resolve local run references independently; compare portable UID and clocks.
    runs = [dict(r) for r in conn.execute('SELECT * FROM task_runs WHERE task_id=?', (task_id,))]
    if any(not isinstance(r['uid'], str) or not r['uid'] or r.get('deleted_at') is not None
           or type(r['started_at']) is not int or type(r['ended_at']) is not int
           or not 0 <= r['started_at'] <= r['ended_at'] <= last['created_at']
           or r['status'] in (None, 'running') or not isinstance(r['outcome'], str)
           for r in runs):
        return None
    completed = [r for r in runs if r['id'] == last['run_id']]
    if len(completed) != 1:
        return None
    run = completed[0]
    if (not isinstance(run['uid'], str) or not run['uid'] or run.get('deleted_at') is not None
            or run['status'] not in ('done', 'completed') or run['outcome'] != 'completed'
            or not run['started_at'] <= task['completed_at'] <= run['ended_at'] <= last['created_at']):
        return None
    if any(r['ended_at'] > run['ended_at'] for r in runs):
        return None
    return {'source': original, 'completion': completion, 'completed_at': task['completed_at'],
            'run': {k: run[k] for k in ('uid', 'status', 'outcome', 'started_at', 'ended_at')},
            'assignee': task['assignee']}


def superseded(live, shadow, task_id, source_uid):
    """Called only after exact immutable rejected-command custody verifies."""
    current = _proof(live, task_id, source_uid)
    return current is not None and current == _proof(shadow, task_id, source_uid)
