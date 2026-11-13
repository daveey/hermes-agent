"""[daveey] Hand back a scheduled fire the gateway never admitted, counting dial-failure holds.

A fire is held (not booked) when nothing was submitted: the gateway was draining, starting or
absent, or its socket could not be opened at all (``connect_failed``). A dial failure is the one
hold that can also mean "the gateway is dead", so consecutive ``connect_failed`` holds are
counted in ``last_fire_error.connect_holds``; ``cron.scheduler_authority`` books the fire FAILED
once ``CONNECT_HOLD_LIMIT`` holds have passed. Any other hold restarts the count, and a successful
run clears ``last_fire_error`` (``cron.jobs._record_run_outcome``).
"""
from __future__ import annotations

from typing import Any, Optional

CONNECT_FAILED = "connect_failed"


def connect_holds(job: Optional[dict[str, Any]]) -> int:
    """Consecutive dial-failure holds recorded on *job*'s stored record."""
    stamp = (job or {}).get("last_fire_error")
    count = stamp.get("connect_holds") if isinstance(stamp, dict) else None
    return count if isinstance(count, int) and count > 0 else 0


def release_unadmitted_fire(
    job_id: str, slot: Optional[str], detail: str, *, expected_fire_owner: Optional[str] = None,
    reason_code: Optional[str] = None,
) -> bool:
    """Hand back a scheduled fire the gateway never admitted: ``next_run_at`` returns to *slot* so
    the next ready tick fires it, the fire claim, pending slot and run claim are dropped, a finite
    one-shot's ``claim_dispatch`` bump is undone, and ``last_fire_error`` records why (with the
    dial-failure hold count when *reason_code* is ``connect_failed``).
    ``last_status``/``failure_streak``/``last_run_at`` are left alone: nothing ran. Fenced on
    *expected_fire_owner*: a claim another fire took since is not ours to release. True iff
    released."""
    from cron import extra_fire
    from cron.jobs import _hermes_now, _with_job, save_jobs

    def apply(jobs, _i, job):
        claim = job.get("fire_claim")
        if expected_fire_owner is not None and (not isinstance(claim, dict)
                                                or claim.get("by") != expected_fire_owner):
            return False
        kind = job.get("schedule", {}).get("kind")
        if slot and kind in {"cron", "interval"}:
            job["next_run_at"] = slot
        repeat = job.get("repeat") or {}
        times = repeat.get("times")
        if (kind == "once" and isinstance(times, int) and times > 0
                and not extra_fire.is_extra(job) and int(repeat.get("completed") or 0) > 0):
            repeat["completed"] = int(repeat["completed"]) - 1
        job["fire_claim"] = None
        job.pop("pending_slot", None)
        if job.get("run_claim") is not None:
            job["run_claim"] = None
        stamp: dict[str, Any] = {"at": _hermes_now().isoformat(), "detail": str(detail or "")[:500]}
        if reason_code == CONNECT_FAILED:
            stamp["connect_holds"] = connect_holds(job) + 1
        job["last_fire_error"] = stamp
        save_jobs(jobs)
        return True

    return _with_job(job_id, apply, False)
