"""Creation-time administrative policy, never execution or release authority. [daveey]"""
from __future__ import annotations

import json

from hermes_cli.kanban_actionability import owner, positive_time, project, valid_awaiting
from hermes_cli.kanban_actionability_selection import LANDINGS


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


# Each of these leaves (or re-enters) the todo phase: an obligation never carries across one.
EPISODE_EXITS = frozenset({'promoted', 'promoted_manual', 'claimed', 'reclaimed', 'scheduled', 'continued',
                           'blocked', 'unblocked', 'completed', 'archived', 'review_requested',
                           'failure_finalized', 'spend_budget_triaged', 'reopened'})


def _todo_entry(kind, payload):
    """Sources that put (or keep) a card waiting in todo on its parents."""
    return kind in ('dependency_wait', 'linked', 'unlinked') or (
        kind in ('unblocked', *LANDINGS) and payload.get('status') == 'todo')


def intent_for(task, kind, payload, *, now):
    """Derive once from source clocks; the same pure function verifies recorded intent."""
    if not isinstance(payload, dict):
        return None
    timed = kind == 'scheduled' or (kind == 'continued' and payload.get('status') == 'scheduled')
    todo = _todo_entry(kind, payload)
    if not timed and kind != 'blocked' and not todo:
        return None
    # link_tasks admits a running child's handoff only from its owner. Ordinary triage edges
    # are also dependency waits; neither clock changes status or supplies release authority.
    if (kind in ('linked', 'unlinked') and task.get('status') == 'triage'
            and task.get('block_kind') == 'lane_gate'):
        return None  # the lane has its own source-fixed clock; an edge must not replace it
    handoff = kind in ('linked', 'unlinked') and task.get('status') in ('running', 'triage')
    status = 'scheduled' if timed else (task['status'] if handoff else (
        'todo' if todo or payload.get('kind') == 'assignment' else 'blocked'))
    if task.get('status') != status:
        return None
    wake = positive_time(payload.get('wake_at')) if timed else None
    deadline = wake + 600 if wake else (now + 86400 if not timed else None)
    # An edge change on a card already waiting in todo keeps that episode's frozen
    # obligation, overdue included; it can only ever be earlier, never renewed.
    carried = positive_time(payload.get('actionability_carried_deadline')) if kind in ('linked', 'unlinked') else None
    if carried and deadline:
        deadline = min(deadline, carried)
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
    elif deadline is None or (deadline <= now and deadline != carried):
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


def _carried(conn, task_id, task):
    """The current todo episode's frozen obligation, read in the writer's txn before this event."""
    from hermes_cli.kanban_actionability_selection import select
    events = [dict(r) for r in conn.execute(
        'SELECT * FROM task_events WHERE task_id=? ORDER BY id', (task_id,))]
    key = lambda e: (e['created_at'], e['id'])  # the census source order
    source = select(task['status'], events, order_key=key)
    if source is None or source['kind'] == 'claimed' or any(e['kind'] in EPISODE_EXITS and key(e) > key(source) for e in events):
        return None
    try:
        data = json.loads(source['payload'] or 'null')
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    intent = validated_intent(task, {'kind': source['kind'], 'created_at': source['created_at'], 'data': data})
    # Provenance: the synced event uid, or the local event id on an unsynced board.
    return (intent['deadline_at'], source.get('uid') or str(source['id'])) if intent and intent['status'] == task['status'] else None


def stamp(conn, task_id, kind, payload, *, now):
    """Freeze ordinary administrative policy with the actual source event clock."""
    if kind not in ('scheduled', 'blocked', 'continued', 'dependency_wait', 'linked', 'unlinked', 'unblocked', *LANDINGS):
        return payload
    original = payload
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    # A confirmed Board wait_on receipt: the Board linked ``parent`` and parked the card in todo in that
    # receipt's transaction, and this replica still shows the triage it left. Freeze the wait as it landed.
    receipt = bool(kind == 'spend_review_settled' and isinstance(payload, dict) and payload.get('verdict') == 'wait_on'
                   and payload.get('status') == 'todo' and _text(payload.get('parent')) and payload['parent'] != task_id
                   and row is not None and row['status'] == 'triage')
    if receipt and row is not None:
        row = {**dict(row), 'status': 'todo', 'block_kind': None, 'human_request': None}
    handoff = row is not None and row['status'] in ('running', 'triage') and kind in ('linked', 'unlinked')
    if kind in ('linked', 'unlinked', 'unblocked', *LANDINGS) and (
            row is None or not (row['status'] == 'todo' or handoff) or not isinstance(payload, dict)):
        return payload  # a todo entry or the owning run's handoff edge; every other status keeps its own source
    if isinstance(payload, dict):
        from hermes_cli.kanban_db_sync import run_uid
        from hermes_cli.kanban_actionability_parents import snapshot
        parents = [r[0] for r in conn.execute('SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id', (task_id,))]
        if receipt:
            parents = sorted({*parents, payload['parent']})
        if kind in ('linked', 'unlinked', 'unblocked', *LANDINGS) and not parents:
            return original  # no parent to wait on: not a dependency wait, so nothing to freeze
        payload = {**payload, 'actionability_parent_ids': parents,
                   'actionability_parent_states': snapshot(conn, parents),
                   'actionability_run_uid': run_uid(conn, row['current_run_id']) if row else None}
        carried = _carried(conn, task_id, dict(row)) if kind in ('linked', 'unlinked') and row else None
        if carried:
            payload['actionability_carried_deadline'], payload['actionability_carried_from'] = carried
        elif handoff and kind == 'unlinked':
            return original  # unlink has no owning-run check: it keeps a handoff clock, never starts one
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
