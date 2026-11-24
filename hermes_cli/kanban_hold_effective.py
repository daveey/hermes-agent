"""Current guidance over a stable hold episode; notification revision is not clearance."""
from __future__ import annotations


def current(conn, task_id: str):
    """Read actual fences, not the original spend payload that later fences annotate."""
    import hashlib
    import json
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli.kanban_hold_audience import describe
    from hermes_cli.kanban_refusal_receipt import restricted
    from hermes_cli.kanban_scope_renewal import covered, key
    from hermes_cli.kanban_sync_divergence import _event_order
    stop = decision.pending_episode(conn, task_id)
    if stop is None:
        return None
    episode = stop.get('uid') or str(stop['id'])
    payload = kb._json_dict(stop['payload'])
    safety = restricted(conn, task_id)
    cause = 'safety_refusal' if safety else 'scope_stopped' if decision.execution_restricted(conn, task_id) else None
    source = stop
    if cause:
        renewed = covered(conn, task_id)
        candidates = [dict(r) for r in conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND kind IN "
            "('spend_budget_triaged','decision_hold_restricted')", (task_id,))]
        candidates = [e for e in candidates if kb._json_dict(e['payload']).get('cause') == cause
                      and (safety or key(e) not in renewed)]
        candidates.extend(e for e in decision.receipt_restrictions(conn, task_id, safety_only=safety)
                          if safety or key(e) not in renewed)
        if candidates:
            source = max(candidates, key=_event_order)
            payload.update(kb._json_dict(source['payload']))
        payload['cause'] = cause
    payload.update(task_id=task_id, episode=episode, authority=describe(conn, task_id))
    # Producer routing is delivery bookkeeping, not a guidance change.
    payload.pop('producer', None)
    revision = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return source, {**payload, 'revision': revision}


def previous(conn, task_id: str, payload: dict):
    """A pre-revision notice counts only when it already carries identical safe guidance."""
    import json
    from hermes_cli import kanban_db as kb
    rows = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='spend_budget_decision_ready' "
                        "ORDER BY id DESC", (task_id,))
    wanted = {k: v for k, v in payload.items() if k != 'revision'}
    for row in rows:
        prior = kb._json_dict(row['payload'])
        if prior.get('revision') == payload['revision']:
            return dict(row)
        prior = {k: v for k, v in prior.items() if k not in ('revision', 'producer')}
        if json.dumps(prior, sort_keys=True) == json.dumps(wanted, sort_keys=True):
            return dict(row)
    return None
