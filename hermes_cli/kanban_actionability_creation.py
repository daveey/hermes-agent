"""Creation-time administrative policy, never execution or release authority. [daveey]"""
from __future__ import annotations

import json

from hermes_cli.kanban_actionability import owner, positive_time, project, valid_awaiting


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def intent_for(task, kind, payload, *, now):
    """Derive once from source clocks; the same pure function verifies recorded intent."""
    if not isinstance(payload, dict):
        return None
    timed = kind == 'scheduled' or (kind == 'continued' and payload.get('status') == 'scheduled')
    if not timed and kind not in ('blocked', 'dependency_wait'):
        return None
    status = 'scheduled' if timed else ('todo' if kind == 'dependency_wait' or payload.get('kind') == 'assignment' else 'blocked')
    if task.get('status') != status:
        return None
    wake = positive_time(payload.get('wake_at')) if timed else None
    deadline = wake + 600 if wake else (now + 86400 if not timed else None)
    human = payload.get('human_request')
    human = _text(human.get('action')) if isinstance(human, dict) else None
    parents = payload.get('actionability_parent_ids') or []
    dependencies = [{'task_id': pid, 'status': None, 'trigger': f'Observe the existing dependency {pid} through authenticated lifecycle gates'}
                    for pid in parents] if isinstance(parents, list) and all(_text(pid) for pid in parents) else []
    reason = None
    if owner(task.get('assignee'))['kind'] == 'unavailable':
        reason = 'owner_missing'
    elif timed and not wake:
        reason = 'wake_invalid'
    elif timed and not valid_awaiting(payload.get('awaiting')):
        reason = 'producer_malformed'
    elif deadline is None or deadline <= now:
        reason = 'deadline_not_future'
    elif (timed and not (payload.get('awaiting') or _text(payload.get('wake_check')))) or (not timed and not human and not dependencies):
        reason = 'trigger_missing'
    row = project({'task': task, 'source_at': now, 'wake_at': wake,
                   'deadline_at': deadline, 'human_step': human, 'dependencies': dependencies,
                   'awaiting': payload.get('awaiting'), 'probe': _text(payload.get('wake_check'))}, now=now)
    return {'version': 1, 'policy': 'scheduled-wake-grace-v1' if timed else 'blocked-admin-review-v1',
            'status': status, 'source_kind': kind, 'owner': row['owner'],
            'action': row['action'], 'instruction': row['instruction'], 'blocker': row['blocker'],
            'first_observed': now, 'wake_at': wake, 'deadline_at': None if reason else deadline,
            'invalid_reason': reason, 'execution_authority': False}


def stamp(conn, task_id, kind, payload, *, now):
    """Freeze ordinary administrative policy with the actual source event clock."""
    if kind not in ('scheduled', 'blocked', 'continued', 'dependency_wait'):
        return payload
    original = payload
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if isinstance(payload, dict):
        from hermes_cli.kanban_db_sync import run_uid
        from hermes_cli.kanban_actionability_parents import snapshot
        parents = [r[0] for r in conn.execute('SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id', (task_id,))]
        payload = {**payload, 'actionability_parent_ids': parents,
                   'actionability_parent_states': snapshot(conn, parents),
                   'actionability_run_uid': run_uid(conn, row['current_run_id']) if row else None}
    intent = intent_for(dict(row), kind, payload, now=now) if row else None
    return {**payload, 'actionability_intent': intent} if intent is not None else original


def validated_intent(task, source, *, parent_ids=None):
    """Return exactly supported source-bound bytes, not coercively equal malformed data."""
    data = source['data']
    if parent_ids is not None and data.get('actionability_parent_ids') != parent_ids:
        return None
    human = task.get('human_request')
    if isinstance(human, str):
        try:
            human = json.loads(human)
        except ValueError:
            return None
    if (data.get('human_request') or None) != (human or None):
        return None
    intent = data.get('actionability_intent')
    expected = intent_for(task, source['kind'], data, now=source['created_at'])
    # JSON comparison distinguishes True/1 and 1/1.0; unknown fields/policies fail closed.
    if expected is None or expected['invalid_reason'] or not isinstance(intent, dict):
        return None
    if json.dumps(intent, sort_keys=True) != json.dumps(expected, sort_keys=True):
        return None
    return intent
