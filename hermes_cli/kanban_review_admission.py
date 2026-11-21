"""Host-local, deadline-bearing review admission before claims. [daveey]"""
from __future__ import annotations

import logging

from hermes_cli import kanban_review_model as review

EVENT = "review_route_deferred"
REASON = "review_provider_wall"


def hold(conn, task_id, result, *, lane, dry_run):
    """Review-owned preclaim admission: walls defer and structural refusals escalate.

    Resolve with the same host-local policy as spawn. Refusals allocate no run
    and preserve failure budgets; only the home counts structural rejections.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_model_tier import load_tier_config

    try:
        task = kb.get_task(conn, task_id)
        if task is None or not review.is_review_run(conn, task, lane):
            return False
        review.resolve_review_model(conn, task, load_tier_config())
    except review.ReviewRouteDeferred as exc:
        payload = {"reason": REASON, "until": int(exc.until)}
    except review.ReviewModelUnavailable as exc:
        result.respawn_guarded.append((task_id, "review_model_unavailable"))
        if not dry_run:
            record_refusal(conn, task_id, exc, result, lane=lane)
        return True
    except Exception:
        # This is an optional preflight, not the per-card failure boundary.
        # Spawn retains structural/error accounting and lets siblings proceed.
        logging.getLogger(__name__).exception("Review preflight failed for %s; leaving failure to spawn", task_id)
        return False
    else:
        return False
    result.clock_held[task_id] = (REASON, payload["until"])
    result.respawn_guarded.append((task_id, REASON))
    if not dry_run:
        with kb.write_txn(conn):
            seen = [kb._json_dict(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (task_id, EVENT))]
            if payload not in seen:
                kb._append_event(conn, task_id, EVENT, payload)
    return True


def record_refusal(conn, task_id, exc, result, *, lane):
    """Home-owned, task-scoped refusal accounting; never allocate or finish a run."""
    import time
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wake_check import foreign_copy

    if lane not in ('ready', 'review') or foreign_copy(conn, task_id):
        return  # never end a running source; its owner/finalizer retains custody
    now = int(time.time())
    error = str(exc)[:500]
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task is None or task.status != lane or task.claim_lock or foreign_copy(conn, task_id):
            return
        last = conn.execute(
            "SELECT kind, payload, created_at FROM task_events WHERE task_id=? "
            "AND kind IN ('review_admission_refused', 'claimed', 'review_requested', 'unblocked', 'assigned') "
            "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        prior = kb._json_dict(last['payload']) if last and last['kind'] == 'review_admission_refused' else {}
        same = prior.get('error') == error and prior.get('source_run') == task.current_run_id
        if same and now - last['created_at'] < kb._resolve_rate_limit_cooldown_seconds():
            return
        count = int(prior.get('deferrals', 0)) + 1 if same else 1
        payload = {'admission_refusal': 'review_model_unavailable', 'error': error,
                   'deferrals': count, 'source_run': task.current_run_id, 'infrastructure': True,
                   'retry_status': lane, 'preclaim': True}
        kb._append_event(conn, task_id, 'review_admission_refused', payload)
        exhausted = isinstance(exc, review.ReviewModelsExhausted) or count >= review.DEFERRAL_LIMIT
        if exhausted:
            conn.execute("UPDATE tasks SET status='triage', block_kind='transient', human_request=NULL, "
                         "last_failure_error=? WHERE id=? AND status=? AND claim_lock IS NULL", (error, task_id, lane))
            kb._append_event(conn, task_id, 'gave_up', {**payload, 'kind': 'transient', 'sticky': True,
                             'reason_code': 'review_model_unavailable', 'trigger_outcome': 'review_admission_refused'})
            result.auto_blocked.append(task_id)


def pickup_hold(replica, card):
    """Read-only twin of :func:`hold`: the wall's ``until`` when this machine's dispatcher would defer
    ``card``, else None. Records nothing; any fault returns None (fail open)."""
    try:
        from types import SimpleNamespace
        result = SimpleNamespace(clock_held={}, respawn_guarded=[])
        if not hold(replica, card["id"], result, lane=card["status"], dry_run=True):
            return None
        held = result.clock_held.get(card["id"])
        return None if not held else held[1]
    except Exception:  # health: allow BLE001 -- a hold prediction must never stop a pickup
        logging.getLogger(__name__).warning(
            "kanban review admission: pickup hold check failed for %s; copying ungoverned",
            card["id"] if card is not None else None, exc_info=True)
        return None
