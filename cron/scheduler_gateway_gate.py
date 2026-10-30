"""Tick gate: agent jobs need a ready gateway; a tick outside the gateway holds them until there is one.

``run_canonical_job`` reaches the owner through ``connect_gateway`` → ``ensure_gateway_runtime``.
Every tick whose process has no gateway owner bound (``gateway.session_cron.owner_for_home``) dials
out that way: the system-crontab ``hermes cron tick`` (headless) and the Desktop ``serve`` ticker,
which takes over a store whenever no gateway pid owns it, including the seconds a ``hermes update``
restarts the gateway. Only the gateway's own ticker admits in-process and skips this gate.

"Not ready" is every outcome but ``ready``, plus a live update fence: ensure would answer
``draining``/``update_paused`` (or wait a whole deadline on a ``starting`` owner) and the run would
be booked FAILED, consuming the slot and growing ``failure_streak`` (hermes-hub 2026-10-29/30, cron
6b4342ad83d9: three FAILED runs, one per update restart). Held agent jobs are not run, not failed,
and their due instant does not move: they fire on the first tick that finds the gateway ready.

``may_start``: an interactive owner (the Desktop ticker) may still let ensure start an ABSENT
gateway outside an update. A headless tick never spawns one: every crontab tick would leave a stray
daemon behind (headless surfaces refuse rather than spawn, the same policy as approvals).
"""
import logging
import os

logger = logging.getLogger(__name__)

GATEWAY_DOWN_HINT = (
    "the gateway is not running, so agent jobs cannot fire; they run at the next tick once it is "
    "up. Start it with `hermes gateway start` (or `hermes gateway install` for a service)."
)
UPDATE_HINT = (
    "a Hermes update is in progress (gateway update_paused), so agent jobs cannot fire; they run "
    "at the first tick after the update."
)
# A crontab tick has a minute; a gateway still starting answers within a few seconds or not at all.
_HEADLESS_PROBE_TIMEOUT_S = 5.0


def _update_in_progress(home) -> bool:
    """The same update fence ``ensure_gateway_runtime`` consults (profile, process, install root)."""
    from hermes_cli.gateway_runtime import _update_fenced
    from hermes_constants import get_default_hermes_root, get_process_hermes_home

    try:
        return _update_fenced({home, get_process_hermes_home(), get_default_hermes_root()})
    except Exception:  # ensure judges an unreadable fence itself; never stop the tick here
        logger.debug("update fence check failed for %s", home, exc_info=True)
        return False


def _gateway_unavailable(home, *, may_start: bool = False) -> str | None:
    """Why this tick must hold its agent jobs, or None when they may dispatch. An update fence holds
    (ensure answers update_paused even over a running gateway); otherwise ``ready`` dispatches,
    ``absent`` dispatches only when *may_start*, and starting/draining/inaccessible/incompatible
    hold (ensure would wait a whole deadline, or no owner answers), never booked as a run."""
    from hermes_cli.gateway_runtime import discover_gateway_endpoint

    if os.environ.get("HERMES_TUI_GATEWAY_URL", "").strip():
        return None  # explicit remote gateway: connect_gateway never spawns for it
    if _update_in_progress(home):
        return UPDATE_HINT
    observed = discover_gateway_endpoint(home, timeout=_HEADLESS_PROBE_TIMEOUT_S)
    if observed.state == "ready":
        return None
    if observed.state == "absent":
        return None if may_start else GATEWAY_DOWN_HINT
    reason = observed.state + (f" ({observed.reason_code})" if observed.reason_code else "")
    return (f"the gateway is {reason}, so agent jobs cannot fire; they run at the next tick once it "
            "is ready. Check it with `hermes gateway status`.")


def _note_fire_refused(job_ids, detail: str) -> None:
    """Stamp ``last_fire_error`` (shown by ``hermes cron list``) and drop the dispatch marks the
    due scan just wrote: the slot was never handed over, so nothing must "restore" or "reclaim" it."""
    from cron.jobs import _hermes_now, _jobs_lock, load_jobs, save_jobs

    ids = set(job_ids)
    with _jobs_lock():
        jobs = load_jobs()
        for job in jobs:
            if job["id"] not in ids:
                continue
            job["last_fire_error"] = {"at": _hermes_now().isoformat(), "detail": detail[:500]}
            job.pop("pending_slot", None)
            if job.get("run_claim") is not None:
                job["run_claim"] = None
        save_jobs(jobs)


def refuse_agent_jobs_without_gateway(due_jobs: list, *, may_start: bool = False) -> list:
    """Return the due jobs this tick may dispatch; agent jobs are held back (skipped, not failed)
    when no ready gateway serves this home. Logs once per tick, not once per job."""
    from hermes_constants import get_hermes_home

    agent_jobs = [job for job in due_jobs if not job.get("no_agent")]
    if not agent_jobs:
        return due_jobs
    home = get_hermes_home().resolve()
    hint = _gateway_unavailable(home, may_start=may_start)
    if hint is None:
        return due_jobs
    ids = [str(job["id"]) for job in agent_jobs]
    _note_fire_refused(ids, f"Skipped: {hint}")
    logger.warning(
        "Cron tick for %s skipped %d agent job(s) (%s): %s",
        home, len(ids), ", ".join(ids), hint)
    return [job for job in due_jobs if job.get("no_agent")]


def tick_needs_gate(headless: bool) -> bool:
    """True for every tick whose agent jobs would dial out: headless, or no gateway owner bound in
    this process (the Desktop ``serve`` ticker, a standalone in-process ticker). Decided by the
    owner binding itself rather than by which entry point ticks, so a new ticker path is gated."""
    if headless:
        return True
    from gateway import session_cron
    from hermes_constants import get_hermes_home

    return session_cron.owner_for_home(get_hermes_home()) is None
