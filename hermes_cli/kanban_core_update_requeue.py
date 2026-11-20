"""A hermes-sync core-update requeue is a pause, not a crash. [daveey]

``hermes-sync``'s core drain (``reclaim_core_update_workers`` in the state repo) requeues every
restart-safe worker with ``hermes kanban reclaim <task> --reason CORE_UPDATE_REQUEUE_REASON`` just
before ``hermes update``. The one-shot PR-owner handoffs (an orderly dependency park, a
review checkpoint) are consumed by "the next execution"; without this module that requeued run
counted as one, so the guard then held the owner ``active_pr`` with no worker (2026-11-12,
t_01a1ce8aff1f09c1 on worker-5). A requeued run is skipped as if it never ran.

Only an exact manual reclaim with that reason, whose run ended ``reclaimed`` and carries no
independent crash or timeout evidence, qualifies. A same-run SIGTERM diagnostic matching
its successful host-local updater termination receipt is not an independent crash. Any other reclaim, crash or timeout still consumes the
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

# Optional attribution for the explicit worker interruption route (state ``worker-core-update
# --interrupt-worker-runs``). Routine worker updates never reclaim; legacy events without it
# are "unknown". hermes-sync probes ``kanban reclaim --help`` for both flags before signalling.
URGENCY_SOURCES = ("explicit_operator",)
_HEX = frozenset("0123456789abcdef")


def reclaim_attribution(reason, urgency_source, interrupt_target) -> dict:
    """Validated payload fields for a reclaim: ``{}`` (legacy) or both fields; else ValueError."""
    if urgency_source is None and interrupt_target is None:
        return {}
    if urgency_source is None or interrupt_target is None:
        raise ValueError("reclaim attribution needs both --urgency-source and --interrupt-target")
    if type(urgency_source) is not str or urgency_source not in URGENCY_SOURCES:
        raise ValueError(f"reclaim attribution: unknown urgency source {urgency_source!r}")
    if (type(interrupt_target) is not str or len(interrupt_target) != 40
            or not set(interrupt_target) <= _HEX):
        raise ValueError("reclaim attribution: --interrupt-target must be a lowercase 40-hex commit")
    if reason != CORE_UPDATE_REQUEUE_REASON:
        raise ValueError(f"reclaim attribution only applies to reason {CORE_UPDATE_REQUEUE_REASON!r}")
    return {"urgency_source": urgency_source, "interrupt_target": interrupt_target}


def is_core_update_requeue(conn: sqlite3.Connection, run) -> bool:
    """True only for a run the core updater's restart-safe requeue closed."""
    from hermes_cli import kanban_db as kb

    if run["outcome"] != "reclaimed" or run["ended_at"] is None:
        return False
    events = list(conn.execute(
        "SELECT kind, payload, created_at FROM task_events WHERE task_id = ? AND run_id = ? "
        f"AND kind IN ('reclaimed', {', '.join('?' * len(_FAILURE_KINDS))})",
        (run["task_id"], run["id"], *_FAILURE_KINDS),
    ))
    reclaims = [e for e in events if e["kind"] == "reclaimed"]
    if not reclaims:
        return False
    for event in reclaims:
        payload = kb._json_dict(event["payload"])
        if payload.get("manual") is not True or payload.get("reason") != CORE_UPDATE_REQUEUE_REASON:
            return False
    return all(e["kind"] == "reclaimed" or _updater_term(e, reclaims, run["ended_at"])
               for e in events)


def _updater_term(event, reclaims, ended_at) -> bool:
    """Recognize only the updater's successful TERM, not an independent crash.

    Reclaim and process observation can race and both append a terminal event.
    Require the same run (caller's query), PID, claim and exact end second; missing
    or conflicting evidence stays a failure. No event or run history is rewritten.
    """
    from hermes_cli import kanban_db as kb

    crash = kb._json_dict(event["payload"])
    if (event["kind"] != "crashed" or event["created_at"] != ended_at
            or crash.get("exit_kind") != "signaled" or type(crash.get("exit_code")) is not int
            or crash["exit_code"] != 15 or "worker_lost" in crash or "attempt_note" in crash):
        return False
    for receipt in reclaims:
        p = kb._json_dict(receipt["payload"])
        if (receipt["created_at"] == ended_at
                and p.get("host_local") is True and p.get("termination_attempted") is True
                and p.get("terminated") is True and p.get("sigkill") is False
                and type(p.get("prev_pid")) is int and p["prev_pid"] > 0
                and type(crash.get("pid")) is int and crash["pid"] == p["prev_pid"]
                and isinstance(p.get("prev_lock"), str) and p["prev_lock"].strip()
                and crash.get("claimer") == p["prev_lock"]):
            return True
    return False


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
    # Chronological, not by local id: replica run ids are not time-ordered.
    rows = conn.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY COALESCE(started_at, 0) DESC, id DESC",
                        (task_id,))
    return next((run for run in rows if not _skipped(conn, run)), None)


def latest_ended_run_skipping_requeues(conn: sqlite3.Connection, task_id: str, *, by_ended_at: bool = False):
    """Latest ended run (by id, or by ``ended_at`` when asked), with the same skips."""
    order = "ended_at DESC, id DESC" if by_ended_at else "id DESC"
    rows = conn.execute(f"SELECT * FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY {order}",
                        (task_id,))
    return next((run for run in rows if not _skipped(conn, run)), None)

