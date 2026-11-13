"""A hermes-sync core-update requeue is a pause, not a crash. [daveey]

``hermes-sync``'s core drain (``reclaim_core_update_workers`` in the state repo) requeues every
restart-safe worker with ``hermes kanban reclaim <task> --reason CORE_UPDATE_REQUEUE_REASON`` just
before ``hermes update``. The one-shot PR-owner handoffs (an orderly dependency park, a
review checkpoint) are consumed by "the next execution"; without this module that requeued run
counted as one, so the guard then held the owner ``active_pr`` with no worker (2026-11-12,
t_01a1ce8aff1f09c1 on worker-5). A requeued run is skipped as if it never ran.

Only an exact manual reclaim with that reason, whose run ended ``reclaimed`` and carries no
crash or timeout evidence, qualifies. Any other reclaim, crash or timeout still consumes the
handoff (fail closed). A PR the requeued run reported is a newer PR comment that post-dates every
handoff; when that run itself wrote every such comment, ran as the card's assignee and no real
run was claimed since, the pause grants its lineage one continuation
(:mod:`kanban_requeued_publisher`). Unknown writer provenance, a second writer or a later real
run keeps the hold, and the next real claim consumes the grant (its crash holds again).
Keep the reason in sync with hermes-sync.

Residual: "restart-safe" is a host property, not per-worker quiescence, so a requeued run may
have opened a PR it had not yet reported. The next run sees the requeued run's transcript tail
(kanban_worker_recovery); the risk matches the continued/scheduled handoffs.
"""
from __future__ import annotations

import sqlite3

CORE_UPDATE_REQUEUE_REASON = "hermes-sync core update: restart-safe worker requeue"

_FAILURE_KINDS = ("crashed", "timed_out", "spawn_failed", "gave_up")


def is_core_update_requeue(conn: sqlite3.Connection, run) -> bool:
    """True only for a run the core updater's restart-safe requeue closed."""
    from hermes_cli import kanban_db as kb

    if run["outcome"] != "reclaimed" or run["ended_at"] is None:
        return False
    requeued = False
    for event in conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND run_id = ? "
        f"AND kind IN ('reclaimed', {', '.join('?' * len(_FAILURE_KINDS))})",
        (run["task_id"], run["id"], *_FAILURE_KINDS),
    ):
        if event["kind"] != "reclaimed":
            return False
        payload = kb._json_dict(event["payload"])
        if payload.get("manual") is not True or payload.get("reason") != CORE_UPDATE_REQUEUE_REASON:
            return False
        requeued = True
    return requeued


def requeued_run_ids(conn: sqlite3.Connection, task_id: str) -> set[int]:
    return {run["id"] for run in conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? AND outcome = 'reclaimed'", (task_id,))
        if is_core_update_requeue(conn, run)}


def _skipped(conn: sqlite3.Connection, run) -> bool:
    """A core-update requeue (a pause) or an attested unused Board grant (ran nothing). [daveey]"""
    from hermes_cli.kanban_unused_grant import is_unused_grant

    if run["outcome"] == "reclaimed":
        return is_core_update_requeue(conn, run)
    return run["outcome"] == "released" and is_unused_grant(conn, run)


def latest_run_skipping_requeues(conn: sqlite3.Connection, task_id: str):
    """Latest run, passing over core-update requeues and attested unused grants.

    Any run that started, failed or lacks the unused-grant receipt still counts."""
    rows = conn.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC", (task_id,))
    return next((run for run in rows if not _skipped(conn, run)), None)


def latest_ended_run_skipping_requeues(conn: sqlite3.Connection, task_id: str, *, by_ended_at: bool = False):
    """Latest ended run (by id, or by ``ended_at`` when asked), with the same skips."""
    order = "ended_at DESC, id DESC" if by_ended_at else "id DESC"
    rows = conn.execute(f"SELECT * FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY {order}",
                        (task_id,))
    return next((run for run in rows if not _skipped(conn, run)), None)

