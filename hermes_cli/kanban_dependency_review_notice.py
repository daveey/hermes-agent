"""Diagnostics only: never grant continuation or modify review evidence."""
from hermes_cli import kanban_dependency_review as review


def finished_review_hold(conn, task_id):
    """Describe a held checkpoint whose awaited parents have all finished since park.

    This is called only after active_pr refused the owner. Unlike permission, a
    diagnostic can describe malformed identity/scope without authorizing a retry.
    """
    from hermes_cli import kanban_db as kb

    from hermes_cli.kanban_core_update_requeue import latest_run_skipping_requeues
    # Same run the guard reads: not a core-update requeue, not an unused Board grant.
    latest = latest_run_skipping_requeues(conn, task_id, skip_unused_grants=True)
    row = conn.execute(
        "SELECT e.payload, e.created_at, r.* FROM task_events e "
        "JOIN task_runs r ON r.id=e.run_id WHERE e.task_id=? "
        "AND e.kind='dependency_wait' AND r.task_id=e.task_id "
        "AND r.id=? ORDER BY e.id DESC LIMIT 1", (task_id, latest["id"]),
    ).fetchone() if latest is not None else None
    if row is None:
        return ""
    evidence = kb._json_dict(row["payload"]).get("review_checkpoint")
    if not isinstance(evidence, dict):
        return ""
    parked_at = kb._to_epoch(row["created_at"])
    parents = evidence.get("awaited")
    if parked_at is None or not isinstance(parents, list) or not parents or not all(isinstance(p, str) for p in parents):
        return ""
    finished = []
    for parent in parents:
        run = conn.execute(
            "SELECT r.*, t.status AS task_status FROM task_runs r JOIN tasks t ON t.id=r.task_id "
            "WHERE t.id=? ORDER BY r.id DESC LIMIT 1", (parent,),
        ).fetchone()
        if run is None or run["task_status"] != "done" or (kb._to_epoch(run["ended_at"]) or 0) < parked_at:
            return ""
        finished.append((parent, run))
    return " Finished awaited review(s): " + "; ".join(
        f"{parent}: {_refusal(conn, parent, run, row, evidence, parked_at)}"
        for parent, run in finished
    ) + "."


def _refusal(conn, parent, run, owner, evidence, parked_at):
    from hermes_cli import kanban_db as kb

    if not review._checkpoint_matches_run(evidence, owner):
        return "checkpoint_matches_run refused"
    if review._completed_review_run(conn, parent, parked_at, owner["id"]) is None:
        return "completed_review_run refused (claimed, completed, independent run required)"
    data = kb._json_dict(run["metadata"])
    if not (review._exact_head(data) or review._prefix_repair_head(data)):
        return "exact_head refused (reviewed_sha/head/exact_head must be 40 hex)"
    captured = evidence.get("prs")
    if not isinstance(captured, list) or not all(isinstance(u, str) for u in captured):
        return "checkpoint_prs refused"
    covered = review._reviewed_prs(conn, parent, parked_at, owner["id"], set(captured))
    covered |= review._approval_prs(conn, parent, parked_at, owner["id"], set(captured))
    if not covered:
        return "review_pr_binding refused"
    return "resumes_review scope refused (owner, dependency graph, PR coverage or discharged PRs)"
