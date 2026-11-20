"""Host-local, deadline-bearing review admission before claims. [daveey]"""
from __future__ import annotations

from hermes_cli import kanban_review_model as review

EVENT = "review_route_deferred"
REASON = "review_provider_wall"


def hold(conn, task_id, result, *, lane, dry_run):
    """Defer only proven temporary exhaustion, not missing provenance/policy errors.

    Resolve with the same host-local policy as spawn. Leave status and run/failure
    budgets untouched so another host with an available reviewer can claim it.
    """
    import sqlite3

    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_model_tier import load_tier_config

    task = kb.get_task(conn, task_id)
    if task is None or not review.is_review_run(conn, task, lane):
        return False
    try:
        review.resolve_review_model(conn, task, load_tier_config())
    except review.ReviewRouteDeferred as exc:
        payload = {"reason": REASON, "until": exc.until}
    except (review.ReviewModelUnavailable, sqlite3.Error):
        return False  # retain structural admission error handling
    else:
        return False
    result.clock_held[task_id] = (REASON, payload["until"])
    result.respawn_guarded.append((task_id, REASON))
    if not dry_run:
        with kb.write_txn(conn):
            row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? "
                               "ORDER BY id DESC LIMIT 1", (task_id, EVENT)).fetchone()
            if row is None or kb._json_dict(row[0]) != payload:
                kb._append_event(conn, task_id, EVENT, payload)
    return True
