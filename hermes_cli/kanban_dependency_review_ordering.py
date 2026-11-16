"""Done ordering prerequisites among a review park's awaited parents.

An owner can park on an exact-head review AND an ordering prerequisite (a base-CI repair
card whose result carries no verdict). The ordering handoff defers any set holding a review
result to the review rules, and those demanded that every awaited parent approve, so the
prerequisite that never reviews anything held the owner ``active_pr`` until its PR closed
(2026-11-15 hub t_01a1de3b070ed3b8, PR #3295). A done parent whose latest result is not a
review result neither authorizes nor refuses; :func:`resumes_review` drops it while at least
one review parent remains. [daveey]
"""
from __future__ import annotations


def _parent_result(conn, task_id, parent_id):
    """Latest result of a done parent: its local run, else the foreign-parent mirror."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_move_parent_evidence import result as moved_parent_result

    local = conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,)).fetchone()
    if local is not None:
        if local["status"] != "done":
            return None
        return conn.execute(
            "SELECT metadata, summary FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (parent_id,),
        ).fetchone()
    mirrored = moved_parent_result(conn, task_id, parent_id)
    if not mirrored or mirrored.get("parent_status") != "done":
        return None
    return {"metadata": kb._json_dict(mirrored.get("metadata")), "summary": mirrored.get("summary")}


def ordering_prerequisites(conn, task_id, parents, captured) -> set[str]:
    """Awaited ``parents`` that are done and whose latest result is not a review result.

    A parent with no readable result (no local run, no mirror row) is not one: it stays
    with the review rules and fails closed there.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_dependency_review import is_review_result

    ordering = set()
    for parent in parents:
        result = _parent_result(conn, task_id, parent)
        if result is not None and not is_review_result(
                kb._json_dict(result["metadata"]), result["summary"], captured):
            ordering.add(parent)
    return ordering
