"""Guarded retirement of informational, automation-filed lane backlog. [daveey]

Checks belong to archive_task's writer transaction: an earlier planner snapshot
cannot authorize retiring a card which acquired a worker, ask, or dependent.
"""
from __future__ import annotations

import time

from hermes_cli import kanban_lanes as lanes

# Explicit informational producers only; an arbitrary non-human label is not
# sufficient evidence that a card is an expiring report rather than repair work.
INFORMATIONAL = frozenset({"core-ship-backstop", "model-capacity-watch", "coach-review", "ci-flaky"})
STALE_SECONDS = 7 * 86400


def refusal(conn, task_id, action):
    from hermes_cli.kanban_containment_guard import pending

    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None or not conn.execute(
        f"SELECT 1 FROM tasks WHERE id=? AND {lanes.HELD}", (task_id,)
    ).fetchone():
        return "card is not held at a lane gate"
    if row["created_by"] not in INFORMATIONAL:
        return "card is not from a supported informational automation"
    if row["human_request"]:
        return "card has a human request"
    if row["claim_lock"] or row["worker_pid"] or row["current_run_id"]:
        return "card has execution custody"
    if conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL", (task_id,)).fetchone():
        return "card has a live run"
    if pending(conn, task_id):
        return "card has a containment restriction"
    if conn.execute(
        "SELECT 1 FROM task_links l JOIN tasks t ON t.id=l.child_id "
        "WHERE l.parent_id=? AND t.status NOT IN ('done','archived')", (task_id,)
    ).fetchone():
        return "card has children waiting on it"
    if action.get("basis") != "stale":
        return "unsupported or unverified archive basis"
    gate = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=? AND kind=?",
                        (task_id, lanes.GATED)).fetchone()[0]
    if gate is None or max(row["created_at"], gate) >= time.time() - STALE_SECONDS:
        return "informational card has not been held for more than seven days"
    return None


def archive(conn, _pol, action, row, _state):
    from hermes_cli import kanban_db as kb

    reasons = []

    def check(current):
        why = refusal(current, row["id"], action)
        if why:
            reasons.append(why)
        return why

    if kb.archive_task(conn, row["id"], via="lane-planner", precondition=check,
                       event_extra={"basis": action.get("basis"), "reason": action["reason"]}):
        return None
    return reasons[0] if reasons else "archive did not commit"
