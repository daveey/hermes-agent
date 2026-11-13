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
reschedule that no run has settled yet (the rerun itself, interrupted again) is not rescheduled,
unless the host slept through it (below).

[daveey] Sleep (cron-7dfb141525): a MacBook asleep on battery wakes only in short DarkWakes, so a
restart there cuts the run and the immediate rerun is cut by the same sleep; rerun-once then
dropped the work although the host was fully awake for hours before the next slot. The owner
measures each attempt's awake time (``time.monotonic``, which stops in sleep) and wall time
(``cron/restart_cut.py`` ``AWAKE_FLAG``). A cut rerun that had less than ``SLEPT_AWAKE_SECONDS``
awake, or slept through most of a long attempt, is re-armed again, at most ``MAX_RERUNS`` times per
original cut; an attempt with no measurement keeps rerun-once. Every restart rerun is held while
the host is not fully awake (``host_fully_awake``: never in a DarkWake), and the job's next
scheduled slot settles a rerun still held (``hold_for_wake``).
"""
from __future__ import annotations

import logging
import subprocess
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("cron.jobs")

MARKER_KEY = "restart_rerun"
# A core update's drain + restart takes minutes; a scheduler silent for longer than this was
# stopped, not restarted, and the job's own missed-run policy (grace, catch_up) applies instead.
RESTART_WINDOW_SECONDS = 30 * 60
# A cut rerun with less awake time than this never really ran: the host slept through it.
SLEPT_AWAKE_SECONDS = 10 * 60
# Reruns per original cut (the first rerun included) a sleeping host may spend.
MAX_RERUNS = 3
ITSELF_A_RERUN = "the interrupted attempt was itself a restart rerun"


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def slept_through(awake_seconds: Any, wall_seconds: Any = None) -> bool:
    """Whether a cut attempt with this measured awake/wall time gets a further rerun: under
    ``SLEPT_AWAKE_SECONDS`` awake (too short to have done its work, whether the host slept or was
    restarted again at once; ``MAX_RERUNS`` bounds it), or asleep for at least that long and for
    most of the attempt. An unmeasured attempt (no owner measurement) is not: rerun-once applies."""
    awake, wall = _number(awake_seconds), _number(wall_seconds)
    if awake is None or awake < 0:
        return False
    if awake < SLEPT_AWAKE_SECONDS:
        return True
    asleep = (wall - awake) if wall is not None else 0.0
    return asleep >= SLEPT_AWAKE_SECONDS and asleep > awake


def _reruns(marker: Any) -> int:
    return int(marker.get("reruns") or 1) if isinstance(marker, dict) else 0


def _pmset(*args: str) -> str:
    return subprocess.run(["pmset", "-g", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=5, check=True).stdout


def _lid_closed() -> bool:
    out = subprocess.run(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "1"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=5, check=False).stdout
    return '"AppleClamshellState" = Yes' in out


def _on_macos() -> bool:
    from hermes_platform.host.facts import os_family

    return os_family() == "darwin"


def host_fully_awake() -> bool:
    """False only on macOS when the host is provably not fully awake (a DarkWake or Power Nap):
    no ``UserIsActive`` assertion and not lid-open on AC. Any other platform, and any probe
    failure, reads awake: a held rerun must never wait on a broken probe (the next slot settles
    it anyway)."""
    if not _on_macos():
        return True
    try:
        for line in _pmset("assertions").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "UserIsActive":
                if parts[1] == "1":
                    return True
                break
        return "AC Power" in _pmset("batt") and not _lid_closed()
    except (OSError, subprocess.SubprocessError, ValueError):
        return True


def _parse(value: Any) -> Optional[datetime]:
    from cron.jobs import _parse_aware

    return _parse_aware(value) if isinstance(value, str) and value else None


def _at_or_after(value: Any, claimed: datetime) -> bool:
    parsed = _parse(value)
    return parsed is not None and parsed >= claimed


def _skip_reason(
    job: dict[str, Any], record: dict[str, Any], claimed: datetime, now: datetime,
    restart_cut: bool = False, deferred: bool = False, slept: bool = False,
) -> Optional[str]:
    from cron.jobs import _is_recoverable_error_job, is_job_runnable, is_terminal_job
    from cron.scheduler_ownership import _claim_owner_is_dead

    marker = job.get(MARKER_KEY)
    if isinstance(marker, dict):
        if marker.get("execution_id") == record["id"]:
            return "already rescheduled"
        rescheduled_at = _parse(marker.get("at"))
        # A deferral re-arms a fire that never ran (defer_unadmitted), so it spends no rerun: neither
        # the attempt a deferral leads to nor a deferred rerun (re-armed, still the rerun) is skipped.
        if (not deferred and not marker.get("deferred") and not marker.get("settled")
                and rescheduled_at is not None and claimed >= rescheduled_at):
            # Claimed after a reschedule that no real outcome has settled: the rerun itself. One the
            # host slept through never really ran, so it is re-armed up to MAX_RERUNS times.
            if not slept:
                return ITSELF_A_RERUN
            if _reruns(marker) >= MAX_RERUNS:
                return f"{ITSELF_A_RERUN}, and its {MAX_RERUNS} reruns are spent"
    # A verified restart cut (the owner's own verdict) is settled by its firer or journal recovery,
    # whose uncertainty pause/resume stamps ``edited_at`` and whose claim may still be its own:
    # neither says an operator changed the job or another process will settle it.
    if not restart_cut and _at_or_after(job.get("edited_at"), claimed):
        return "edited since the attempt started"  # pause, resume and run-now included
    claim = job.get("fire_claim")
    if not restart_cut and isinstance(claim, dict) and not _claim_owner_is_dead(claim):
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


def _marker(job: dict[str, Any], record: dict[str, Any], now: str, deferred: bool,
            reruns: int = 1) -> dict[str, Any]:
    """The loop guard written with a reschedule. A deferral marks itself ``deferred`` so it spends no
    rerun, except when it defers a pending rerun: that keeps the rerun's time, still unsettled.
    ``reruns`` counts the reruns this original cut has spent (written only past the first)."""
    if not deferred:
        marker: dict[str, Any] = {"execution_id": record["id"], "at": now}
        if reruns > 1:
            marker["reruns"] = reruns
        return marker
    prior = job.get(MARKER_KEY)
    if (isinstance(prior, dict) and not prior.get("deferred") and not prior.get("settled")
            and _parse(prior.get("at")) is not None):
        kept = {"execution_id": record["id"], "at": prior["at"]}
        if prior.get("reruns"):
            kept["reruns"] = prior["reruns"]
        return kept
    return {"execution_id": record["id"], "at": now, "deferred": True}


def _apply(job: dict[str, Any], record: dict[str, Any], now: str, deferred: bool = False,
           reruns: int = 1) -> None:
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
    job[MARKER_KEY] = _marker(job, record, now, deferred, reruns)


def pending_rerun(job: dict[str, Any]) -> bool:
    """Whether the job's queued fire is a restart rerun no run has claimed yet."""
    from cron import extra_fire

    marker = job.get(MARKER_KEY)
    if not isinstance(marker, dict) or marker.get("settled") or marker.get("deferred"):
        return False
    at = marker.get("at")
    if not at or job.get("next_run_at") != at:
        return False
    if (job.get("schedule") or {}).get("kind") == "once":
        return True
    return job.get("manual_run_at") == at and extra_fire.RESTORE_KEY in job


def hold_for_wake(job: dict[str, Any], scan: Any) -> bool:
    """[daveey] The due scan's hold on a restart rerun while the host is not fully awake (a
    DarkWake): firing it there lets the same sleep cut it again. True holds it this tick.

    A recurring job's held rerun is settled by its next scheduled slot: once that slot is due the
    extra fire is dropped and the slot fires as itself (no double run). A one-shot waits for the
    full wake."""
    from cron import extra_fire

    if not pending_rerun(job) or host_fully_awake():
        return False
    restore = _parse(job.get(extra_fire.RESTORE_KEY))
    if (job.get("schedule") or {}).get("kind") != "once" and restore is not None and restore <= scan.now:
        slot = job[extra_fire.RESTORE_KEY]
        settled = dict(job[MARKER_KEY], settled=True, superseded_by=slot)
        for target in (job, scan.find(job["id"])):
            if target is None:
                continue
            target.pop("manual_run_at", None)
            target.pop(extra_fire.RESTORE_KEY, None)
            target["next_run_at"] = slot
            target[MARKER_KEY] = dict(settled)
        scan.needs_save = True
        logger.warning("cron: pending restart rerun of %s settled by its next slot %s (the host "
                       "never fully woke)", job.get("id"), slot)
        return True  # this tick re-reads nothing stale; the next one fires the slot as itself
    logger.debug("cron: holding restart rerun of %s until the host is fully awake (not in a DarkWake)",
                 job.get("id"))
    return True


def _newest_attempt_id(job_id: str) -> Optional[str]:
    from cron.executions import list_executions

    for row in list_executions(job_id=job_id, limit=20):
        if row.get("status") != "missed":
            return row.get("id")
    return None


def reschedule_interrupted(record: dict[str, Any], *, restart_cut: bool = False,
                           deferred: bool = False, awake_seconds: Any = None,
                           wall_seconds: Any = None) -> bool:
    """Put ``record``'s job back on the schedule for now; True when it was rescheduled.

    ``restart_cut``: the owner itself reported that its restart cut this attempt (``RESTART_CUT_FLAG``
    on the canonical result), and the caller (the attempt's firer or its journal recovery) is the
    one settling it, so the journal and a live claim are no reason to defer. ``deferred``: the
    attempt never ran (refused while draining), so its re-arm spends no rerun (``_marker``).
    ``awake_seconds``/``wall_seconds``: the owner's measurement of the cut attempt; a rerun the host
    slept through (``slept_through``) is re-armed again instead of dropped."""
    from cron.jobs import _MISSING, _hermes_now, _under_fire_fence, _with_job, save_jobs

    job_id = str(record.get("job_id") or "")
    claimed = _parse(record.get("claimed_at"))
    if not job_id or claimed is None:
        return False
    from cron.scheduler_authority import journal_path

    if not restart_cut and journal_path(job_id, record["id"]).exists():
        # An admitted canonical fire: its journal recovery observes the owner's receipt and
        # reschedules it there when the owner says a restart cut it (settle_restart_cut); it must
        # never be raced by a second submit (cron/observer_reconnect.py).
        logger.info("cron: not rescheduling %s (execution %s) here: its admission journal recovery "
                    "settles it", job_id, record["id"])
        return False
    if _newest_attempt_id(job_id) != record.get("id"):
        logger.info("cron: not rescheduling %s (execution %s): a newer attempt exists",
                    job_id, record.get("id"))
        return False

    _fence_reported = [False]  # apply() logged its own skip; a bare False is a fence timeout
    slept = slept_through(awake_seconds, wall_seconds)
    rerun_count = [1]

    def apply(jobs, _i, job):
        _fence_reported[0] = True
        now = _hermes_now()
        reason = _skip_reason(job, record, claimed, now, restart_cut, deferred, slept)
        if reason is not None:
            if reason.startswith(ITSELF_A_RERUN):
                # Expire the pending-rerun guard, not the replay guard. Popping it would let a
                # repeated settlement of this same cut attempt schedule a second rerun.
                job[MARKER_KEY] = {"execution_id": record["id"], "at": now.isoformat(), "settled": True}
                save_jobs(jobs)
            logger.info("cron: not rescheduling %s (execution %s) after restart interruption: %s",
                        job_id, record["id"], reason)
            return False
        marker = job.get(MARKER_KEY)
        rescheduled_at = _parse(marker.get("at")) if isinstance(marker, dict) else None
        if (slept and not deferred and isinstance(marker, dict) and not marker.get("settled")
                and not marker.get("deferred") and rescheduled_at is not None and claimed >= rescheduled_at):
            rerun_count[0] = _reruns(marker) + 1  # a rerun the host slept through, re-armed again
        _apply(job, record, now.isoformat(), deferred, rerun_count[0])
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
        detail = ""
        if rerun_count[0] > 1:
            detail = (f" (deferred rerun {rerun_count[0]}/{MAX_RERUNS}: the host slept through the cut "
                      f"rerun, awake {_number(awake_seconds) or 0:.0f}s of {_number(wall_seconds) or 0:.0f}s)")
        logger.warning("cron: rescheduled %s (execution %s) after restart interruption%s",
                       job_id, record["id"], detail)
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
