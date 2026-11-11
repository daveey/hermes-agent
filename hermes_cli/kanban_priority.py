"""Bound priority inputs and make queue advice conditional on measured lane evidence."""
from __future__ import annotations

from statistics import median

PRIORITY_CAP = 1000


def bounded(value: int) -> int:
    """Keep negative deprioritization, but never permit an escalation arms race."""
    return min(PRIORITY_CAP, int(value))


def bound_reopened(conn, task_id: str) -> None:
    """Bound an opened legacy row in the caller's transaction, retaining its audit."""
    from hermes_cli import kanban_db as kb

    row = conn.execute("SELECT priority FROM tasks WHERE id=? AND status NOT IN ('done','archived')",
                       (task_id,)).fetchone()
    if row is None or row[0] <= PRIORITY_CAP:
        return
    conn.execute("UPDATE tasks SET priority=? WHERE id=?", (PRIORITY_CAP, task_id))
    kb._append_event(conn, task_id, "reprioritized", {
        "previous_priority": row[0], "priority": PRIORITY_CAP, "reason": "legacy priority bounded on reopen",
    })


def lane_evidence(conn, task_id: str) -> dict:
    """Compare effective priorities in the same ready/assignee/placement lane.

    Missing evidence never recommends escalation. Placement policy and runs_on
    distinguish host-bound work from portable work; no global median is used.
    """
    from hermes_cli.kanban_db_dispatch_queries import EFFECTIVE_PRIORITY

    columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    labels = "runs_on" if "runs_on" in columns else "NULL"
    row = conn.execute(
        f"SELECT assignee, status, placement_policy, {labels} AS labels FROM tasks WHERE id=?", (task_id,),
    ).fetchone()
    if row is None or row["status"] != "ready":
        return {}
    rows = conn.execute(
        f"SELECT id, {EFFECTIVE_PRIORITY} AS effective FROM tasks "
        "WHERE status='ready' AND claim_lock IS NULL AND assignee IS ? "
        f"AND placement_policy IS ? AND {labels} IS ?",
        (row["assignee"], row["placement_policy"], row["labels"]),
    ).fetchall()
    own = next((r["effective"] for r in rows if r["id"] == task_id), None)
    if own is None:
        return {}
    middle = median(r["effective"] for r in rows)
    return {"effective_priority": own, "lane_median": middle, "below_lane_median": own < middle}


def advice(task_id: str, evidence: dict | None = None) -> str:
    """Capacity first; a bounded relative bump only with below-median evidence."""
    text = ("check placement and available capacity first; re-place on a capable gateway with free slots. "
            "Do not run a second worker outside the dispatcher or promise a pickup")
    if (evidence or {}).get("below_lane_median") is True:
        text += (f"; this card is below its lane median: consider a bounded relative bump "
                 f"(`hermes kanban edit {task_id} --priority-bump 10`, cap {PRIORITY_CAP})")
    return text
