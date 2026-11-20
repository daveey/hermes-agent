"""Host-local, deadline-bearing review admission before claims. [daveey]"""
from __future__ import annotations

import logging

from hermes_cli import kanban_review_model as review

EVENT = "review_route_deferred"
REASON = "review_provider_wall"


def hold(conn, task_id, result, *, lane, dry_run):
    """Defer only proven temporary exhaustion, not missing provenance/policy errors.

    Resolve with the same host-local policy as spawn. Leave status and run/failure
    budgets untouched so another host with an available reviewer can claim it.
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
    except review.ReviewModelUnavailable:
        return False
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
