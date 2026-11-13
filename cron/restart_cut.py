"""[daveey] A cron attempt its owner gateway's restart cut short is an interruption, never a job failure.

The owner (``gateway/session_cron.py``) stamps ``RESTART_CUT_FLAG`` on the canonical result when
its own shutdown stopped the run, and when the run's admission was settled ``interrupted`` across
the restart (a lost automation turn, ``gateway/session_unattended_unknown.py``). Whichever process
books that result settles it here: the attempt's firer (its run tail) or, when the firer is gone,
the admission journal's recovery in the new gateway (``scheduler_authority.reconcile_pending``).
The job keeps its failure streak, no failure notice is sent, and the job runs again once
(``cron/restart_rerun.py``: an extra fire that keeps the next slot, a one-shot re-armed with its
repeat budget given back). A rerun cut again is not rerun a second time, unless the owner's clock
(``AWAKE_FLAG``) shows it was awake under ten minutes or the host slept through most of it: then
it is re-armed, up to three reruns.

A fire the owner refused because it was draining for a restart was never admitted: it is deferred
to the next gateway (``defer_unadmitted``), not failed.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("cron.jobs")

RESTART_CUT_FLAG = "_restart_cut"
# [daveey] The owner's clock of a cut attempt, riding the canonical result beside RESTART_CUT_FLAG:
# ``{"awake": <time.monotonic delta>, "wall": <time.time delta>}`` in seconds. The monotonic clock
# stops while the host sleeps, so a rerun the host slept through is re-armed (restart_rerun.py).
AWAKE_FLAG = "_run_clock"
RESTART_CUT_REASON = "Interrupted by a gateway restart before the run finished"
# Owner refusals that mean "not ready yet, retry on the next gateway", never "this job is refused".
NOT_READY_REFUSALS = frozenset({"runtime_draining"})


def not_ready_refusal(exc: BaseException) -> Optional[str]:
    """The not-ready reason of an owner's refusal of ``cron.submit`` (in-process ``RuntimeStoreError``
    or a forwarded ``GatewayRPCError``, whose ``str`` is the reason code), else None."""
    reason = getattr(exc, "reason", None) or str(exc)
    return reason if reason in NOT_READY_REFUSALS else None


def _detail(error: Any) -> str:
    error = str(error or "").strip()
    return RESTART_CUT_REASON if not error or error == RESTART_CUT_REASON else f"{RESTART_CUT_REASON} ({error})"


def _reschedule(job_id: str, execution_id: str, *, deferred: bool = False,
                clock: Optional[dict[str, Any]] = None) -> bool:
    from cron.executions import get_execution
    from cron.restart_rerun import reschedule_interrupted

    record = get_execution(execution_id)
    if record is None or record.get("job_id") != job_id:
        return False
    clock = clock if isinstance(clock, dict) else {}
    try:
        return reschedule_interrupted(record, restart_cut=True, deferred=deferred,
                                      awake_seconds=clock.get("awake"), wall_seconds=clock.get("wall"))
    except Exception:  # health: allow BLE001 -- the attempt is already booked; a missed rerun is logged
        logger.warning("cron: rescheduling %s after its restart cut failed", job_id, exc_info=True)
        return False


def _retire_journal(job_id: str, execution_id: str) -> None:
    from cron.scheduler_authority import retire_terminal_journal

    try:
        # Lifts this fire's own uncertainty pause (an observer that lost the draining owner) first,
        # so the rerun below is not refused as "paused".
        retire_terminal_journal(job_id, execution_id)
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("Cron terminal pause retirement deferred: %s", execution_id, exc_info=True)


def settle_restart_cut(job: dict[str, Any], execution_id: str, error: Any, fire_owner: Optional[str],
                       *, recovered: bool = False) -> bool:
    """Book ``execution_id`` as cut by its owner's restart and rerun the job once; True when rerun.

    ``recovered``: booked by journal recovery for a firer that is gone (its ledger row may be
    ``unknown``); otherwise by the firer itself, fenced on its ``fire_owner`` claim. A claim the
    cutting gateway's shutdown already booked (``mark_running_jobs_interrupted``) is not booked again."""
    from cron import scheduler  # the facade tests and the ordinary tail patch
    from cron.executions import recover_receipted_execution

    job_id = job["id"]
    detail = _detail(error)
    clock = job.pop(AWAKE_FLAG, None)
    if isinstance(clock, dict) and isinstance(clock.get("awake"), (int, float)):
        # Recorded per attempt so a slept-through cut is visible in errors.log without the ledger.
        logger.info("Job '%s': cut attempt %s was awake %.0fs of %.0fs", job_id, execution_id,
                    clock["awake"], clock.get("wall") or 0)
    mark_kwargs: dict[str, Any] = {"shutdown_interrupted": True}
    if not job.get("no_agent"):
        mark_kwargs["execution_id"] = execution_id
    if fire_owner is not None:
        mark_kwargs["expected_fire_owner"] = fire_owner
    scheduler.mark_job_run(job_id, False, detail, **mark_kwargs)
    if recovered:
        recover_receipted_execution(execution_id, job_id, success=False, error=detail,
                                    delivery_outcome="suppressed")
    else:
        scheduler.finish_execution(execution_id, success=False, error=detail, delivery_outcome="suppressed")
    _retire_journal(job_id, execution_id)
    rerun = _reschedule(job_id, execution_id, clock=clock)
    logger.warning("Job '%s': %s; %s", job_id, detail,
                   "rerunning it once on the current gateway" if rerun else "not rerun")
    return rerun


def settle_cut_or_lost(job: dict[str, Any], success: bool, error: Any, execution_id: str,
                       fire_owner: Optional[str], execution_token: Any, teardown, ownership_lost) -> bool:
    """The firer's run tail right after ``run_job``: True when the attempt is already settled.

    A run the owner's restart cut is booked as that cut and rerun once, checked before the claim
    because the cutting gateway's shutdown may have cleared it. Otherwise a lost fire claim books
    the lost ownership, as before."""
    from cron import scheduler

    if not success and job.pop(RESTART_CUT_FLAG, False):
        teardown()
        scheduler._consume_interrupted_flag(job["id"], execution_token)  # this books the shutdown cut
        settle_restart_cut(job, execution_id, error, fire_owner)
        return True
    if ownership_lost():
        teardown()
        scheduler._record_fire_ownership_lost(job, fire_owner, execution_id)
        return True
    return False


def _release_own_claim(job_id: str, fire_owner: Optional[str], detail: str) -> None:
    from cron.jobs import _hermes_now, _under_fire_fence, _with_job, save_jobs

    def apply(jobs, _i, job):
        claim = job.get("fire_claim")
        if not isinstance(claim, dict) or claim.get("by") != fire_owner:
            return False
        job["fire_claim"] = None
        job.pop("pending_slot", None)
        if job.get("run_claim") is not None:
            job["run_claim"] = None
        job["last_fire_error"] = {"at": _hermes_now().isoformat(), "detail": detail[:500]}
        save_jobs(jobs)
        return True

    _under_fire_fence(job_id, lambda: _with_job(job_id, apply, False))


def defer_unadmitted(job: dict[str, Any], execution_id: str, detail: str, fire_owner: Optional[str],
                     *, recovered: bool = False) -> bool:
    """A manual fire (or a journaled fire whose owner proved it never admitted it) the owner refused
    while draining: re-arm it for the next gateway's first tick instead of booking a failure.
    Nothing ran, so the streak and last verdict stay. True when it was re-armed."""
    from cron import scheduler
    from cron.executions import recover_receipted_execution

    job_id = job["id"]
    note = f"Not admitted (the gateway was draining for a restart); deferred to the next gateway: {detail}"
    if recovered:
        recover_receipted_execution(execution_id, job_id, success=False, error=note,
                                    delivery_outcome="suppressed")
        _retire_journal(job_id, execution_id)
    else:
        scheduler.finish_execution(execution_id, success=False, error=note, delivery_outcome="suppressed")
    rearmed = _reschedule(job_id, execution_id, deferred=True)
    if not recovered and fire_owner:  # a gone firer's claim names a dead process: the next claim reclaims it
        _release_own_claim(job_id, fire_owner, note)
    logger.warning("Cron %s %s: %s", job_id, "deferred" if rearmed else "not re-armed", note)
    return rearmed
