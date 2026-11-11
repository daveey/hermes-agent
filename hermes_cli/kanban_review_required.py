"""Completion admission for cards with an explicit review requirement."""
from __future__ import annotations

import os


class ReviewRequiredError(ValueError):
    """The current lane cannot approve a review-required card."""


def require_review(conn, task_id: str, expected_run_id: int | None, *, placement_settled: bool = False) -> None:
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    if task is None or not task.review_required or placement_settled:
        return
    if task.status == "review" and expected_run_id is None and not os.environ.get("HERMES_KANBAN_TASK"):
        return  # An operator may approve a pending review without claiming it.
    if (task.status == "running" and task.current_run_id is not None
            and (expected_run_id is None or expected_run_id == task.current_run_id)
            and kb._retry_status_for_run(conn, task_id, task.current_run_id) == "review"):
        return
    raise ReviewRequiredError(
        f"{task_id} requires review: use kanban_request_review, then a reviewer may complete it. "
        "The implementer cannot complete this card, even with --force."
    )
