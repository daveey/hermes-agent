"""Completion admission for cards with an explicit review requirement.

Who may do what on a ``review_required`` card:

* The implementer finishes with ``kanban_request_review``; a reviewer that
  claims the review run then approves with ``kanban_complete``.
* Only an operator (outside a kanban worker run: neither ``HERMES_KANBAN_TASK``
  nor ``HERMES_KANBAN_WORKER_TASK`` is set) may approve a card pending in
  ``review`` without claiming it, or turn the requirement off with
  ``hermes kanban edit <id> --review-required off``. Every kanban worker run,
  dispatched orchestrator workers included, is refused both.
"""
from __future__ import annotations

import os

OPERATOR = "an operator (outside a kanban worker run)"


class ReviewRequiredError(ValueError):
    """The current lane cannot approve a review-required card."""


def worker_identity() -> str:
    """The kanban worker task this process runs for, or ``""`` for an operator."""
    return os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_WORKER_TASK") or ""


def require_review(conn, task_id: str, expected_run_id: int | None, *, placement_settled: bool = False) -> None:
    from hermes_cli import kanban_db as kb

    task = kb.get_task(conn, task_id)
    if task is None or not task.review_required or placement_settled:
        return
    worker = worker_identity()
    if task.status == "review" and expected_run_id is None and not worker:
        return  # An operator may approve a pending review without claiming it.
    if (task.status == "running" and task.current_run_id is not None
            and (expected_run_id is None or expected_run_id == task.current_run_id)
            and kb._retry_status_for_run(conn, task_id, task.current_run_id) == "review"):
        return
    if task.status == "review":  # never tell any lane an in-review card "is not in review"
        stale = ("" if expected_run_id is None else
                 f" Run {expected_run_id} is not its current run: a card pending review has none.")
        if not worker:  # an operator reaches here only with a run id: name the allowed route
            stale += " To approve it as the operator, complete it again without expected_run_id."
        raise ReviewRequiredError(
            f"{task_id} is pending review: a reviewer approves it by claiming the review run; only "
            f"{OPERATOR} may approve it without claiming it.{stale}"
        )
    if not worker:
        raise ReviewRequiredError(
            f"{task_id} requires review and is not in review: send it through kanban_request_review, "
            f"or, as {OPERATOR}, turn the requirement off with "
            f"`hermes kanban edit {task_id} --review-required off`."
        )
    raise ReviewRequiredError(
        f"{task_id} requires review: use kanban_request_review, then a reviewer may complete it. "
        "A kanban worker run cannot complete this card outside review, even with --force."
    )
