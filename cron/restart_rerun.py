"""Rerun a cron execution cut off by its owner process's exit, once, right after recovery.

``recover_interrupted_executions`` books an attempt ``unknown`` when its owner process is provably
gone (gateway restart/update, crash, kill) before a durable terminal state. Without this the job
waits for its next scheduled slot, and a one-shot never runs at all. Here each such attempt puts
its job back on the schedule for ``now``:

* recurring jobs get an extra fire (``cron/extra_fire.py``): the natural next slot is kept in
  ``manual_run_restore_at``, so the rerun spends no occurrence and books nothing missed;
* one-shots are re-armed for ``now`` with the interrupted dispatch's repeat budget given back.

Neither path books a failure. Only attempts whose claiming scheduler died too qualify (the job's
fire claim is gone or names a provably dead process): an external cron worker dying under a live
gateway is that gateway's to settle, and a scheduler down longer than ``RESTART_WINDOW_SECONDS``
was stopped, not restarted (the job's missed-run policy applies). A job deleted, paused or edited
(``edited_at``) since the attempt was claimed, or one that has a newer attempt, is left alone.
``restart_rerun`` on the job
guards loops: the same execution is never rescheduled twice, and an attempt claimed after a
reschedule that no run has settled yet (the rerun itself, interrupted again) is not rescheduled.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("cron.jobs")

MARKER_KEY = "restart_rerun"
# A core update's drain + restart takes minutes; a scheduler silent for longer than this was
# stopped, not restarted, and the job's own missed-run policy (grace, catch_up) applies instead.
RESTART_WINDOW_SECONDS = 30 * 60


def _parse(value: Any) -> Optional[datetime]:
    from cron.jobs import _parse_aware

    return _parse_aware(value) if isinstance(value, str) and value else None


def _at_or_after(value: Any, claimed: datetime) -> bool:
    parsed = _parse(value)
    return parsed is not None and parsed >= claimed


def _skip_reason(
    job: dict[str, Any], record: dict[str, Any], claimed: datetime, now: datetime,
) -> Optional[str]:
    from cron.jobs import _is_recoverable_error_job, is_job_runnable, is_terminal_job
    from cron.scheduler_ownership import _claim_owner_is_dead

    marker = job.get(MARKER_KEY)
    if isinstance(marker, dict):
        if marker.get("execution_id") == record["id"]:
            return "already rescheduled"
        rescheduled_at = _parse(marker.get("at"))
        if rescheduled_at is not None and claimed >= rescheduled_at:
            # Claimed after a reschedule that no real outcome has settled: the rerun itself.
            return "the interrupted attempt was itself a restart rerun"
    if _at_or_after(job.get("edited_at"), claimed):
        return "edited since the attempt started"  # pause, resume and run-now included
    claim = job.get("fire_claim")
    if isinstance(claim, dict) and not _claim_owner_is_dead(claim):
        # Only the attempt's executor died (an external cron worker); the scheduler process
        # that claimed the fire is alive (or unprovable) and settles the outcome itself.
        return "the claiming scheduler is still alive"
    last_alive = max(t for t in (claimed, _parse(claim.get("at") if isinstance(claim, dict) else None),
                                 _parse(job.get("last_run_at"))) if t is not None)
    if (now - last_alive).total_seconds() > RESTART_WINDOW_SECONDS:
        # Down longer than any restart: a stop. The normal missed-run policy decides.
        return "the scheduler was down too long for a restart"
    if not is_terminal_job(job):
        return None if is_job_runnable(job) else "paused"
    if (job.get("schedule") or {}).get("kind") != "once":
        return None if _is_recoverable_error_job(job) else "terminal"
    if not _at_or_after(job.get("last_run_at"), claimed):
        # Retired before this attempt started: not by this attempt's shutdown bookkeeping.
        return "completed before the attempt"
    return None


def _future_slot_pending(job: dict[str, Any], now: str) -> bool:
    """A live record still holding a future occurrence: the cut-off attempt was an extra fire
    (a run-now), so its rerun must be one too and leave that occurrence alone."""
    from cron.jobs import is_terminal_job

    slot = _parse(job.get("next_run_at"))
    return not is_terminal_job(job) and slot is not None and slot > datetime.fromisoformat(now)


def _apply(job: dict[str, Any], record: dict[str, Any], now: str) -> None:
    from cron import extra_fire
    from cron.jobs import _activate_job_record

    if (job.get("schedule") or {}).get("kind") == "once" and not _future_slot_pending(job, now):
        repeat = job.get("repeat")
        if isinstance(repeat, dict):
            times = repeat.get("times")
            completed = int(repeat.get("completed") or 0)
            if times is not None and times > 0 and completed > 0:
                # claim_dispatch spent the budget before the run; the run never finished.
                repeat["completed"] = completed - 1
        _activate_job_record(job)
        job["next_run_at"] = now
    else:
        # The same markers trigger_job writes: an extra fire whose claim restores the slot.
        job[extra_fire.RESTORE_KEY] = extra_fire.restore_target(job)
        job["manual_run_at"] = now
        job["next_run_at"] = now
    # A fire/run claim left by the dead scheduler needs no clearing: its owner is provably dead
    # (checked above), so the next tick's claim reclaims it at once (_claim_owner_is_dead).
    job[MARKER_KEY] = {"execution_id": record["id"], "at": now}


def _newest_attempt_id(job_id: str) -> Optional[str]:
    from cron.executions import list_executions

    for row in list_executions(job_id=job_id, limit=20):
        if row.get("status") != "missed":
            return row.get("id")
    return None


def reschedule_interrupted(record: dict[str, Any]) -> bool:
    """Put ``record``'s job back on the schedule for now; True when it was rescheduled."""
    from cron.jobs import _MISSING, _hermes_now, _under_fire_fence, _with_job, save_jobs

    job_id = str(record.get("job_id") or "")
    claimed = _parse(record.get("claimed_at"))
    if not job_id or claimed is None:
        return False
    from cron.scheduler_authority import journal_path

    if journal_path(job_id, record["id"]).exists():
        # An admitted canonical fire: its journal recovery observes the owner's receipt and
        # must never be raced by a second submit (cron/observer_reconnect.py).
        logger.info("cron: not rescheduling %s (execution %s): its admission journal owns recovery",
                    job_id, record["id"])
        return False
    if _newest_attempt_id(job_id) != record.get("id"):
        logger.info("cron: not rescheduling %s (execution %s): a newer attempt exists",
                    job_id, record.get("id"))
        return False

    _fence_reported = [False]  # apply() logged its own skip; a bare False is a fence timeout

    def apply(jobs, _i, job):
        _fence_reported[0] = True
        now = _hermes_now()
        reason = _skip_reason(job, record, claimed, now)
        if reason is not None:
            logger.info("cron: not rescheduling %s (execution %s) after restart interruption: %s",
                        job_id, record["id"], reason)
            return False
        _apply(job, record, now.isoformat())
        save_jobs(jobs)
        return True

    result = _under_fire_fence(job_id, lambda: _with_job(job_id, apply, missing=_MISSING))
    if result is _MISSING:
        logger.info("cron: not rescheduling %s (execution %s): job no longer exists",
                    job_id, record.get("id"))
        return False
    if result is False and not _fence_reported[0]:
        logger.info("cron: not rescheduling %s (execution %s): its fire fence was busy",
                    job_id, record["id"])
    if result is True:
        logger.warning("cron: rescheduled %s (execution %s) after restart interruption",
                       job_id, record["id"])
    return result is True


def reschedule_interrupted_executions(records: list[dict[str, Any]], owner_gone_reason: str) -> int:
    """Reschedule every attempt booked ``unknown`` because its owner process exited. Bounded per
    record, never raises: the ledger is already settled when this runs."""
    count = 0
    for record in records:
        if record.get("error") != owner_gone_reason:
            continue  # a live-but-wedged owner is not a restart interruption
        try:
            count += reschedule_interrupted(record)
        except Exception:  # health: allow BLE001 -- recovery boundary: the ledger is already settled
            logger.warning("cron: rescheduling %s after restart interruption failed",
                           record.get("job_id"), exc_info=True)
    return count
