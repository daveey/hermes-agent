"""In-process tick admission and dispatch; job execution stays in scheduler.py."""

import concurrent.futures
import contextlib
import logging
import time
from datetime import datetime

from cron import store_health
from cron.constants import is_recurring

logger = logging.getLogger("cron.scheduler")

# Log the yield at most once per episode (reset when the skew changes) to avoid per-interval spam.
_YIELD_LOG_INTERVAL_SECONDS = 3600.0
_last_yield_log: dict[str, object] = {}


def _log_tick_yield_once(reason: str) -> None:
    """Log the yield at error level once per episode (skew signature)."""
    global _last_yield_log
    now = time.monotonic()
    last_reason = _last_yield_log.get("reason")
    last_at = _last_yield_log.get("at", 0.0)
    if last_reason != reason or (now - float(last_at)) >= _YIELD_LOG_INTERVAL_SECONDS:
        logger.error(
            "Cron tick yielded: this process is running stale code (%s) and a "
            "fresher gateway owns the runtime lock — jobs will fire from that "
            "process. Restart this one to reclaim its ticks.",
            reason)
    _last_yield_log = {"reason": reason, "at": now}


def tick(verbose=True, adapters=None, loop=None, sync=True, *, can_dispatch=None,
         headless: bool = False):
    from hermes_cli.backend_retirement import retirement

    # Hold admission through the entire scan/advance/submit handoff. A predicate alone races
    # prepare after the check but before a due job enters the running-job ledger.
    with retirement.work() as admitted:
        if not admitted:
            return 0
        if drain_held():
            from cron.planned_pause import begin
            begin()
            return 0  # as under the admission hold: due jobs stay due and run once after the drain
        with update_admission() as update_admitted:
            if not update_admitted:
                from cron.planned_pause import begin
                begin()
                return 0  # due jobs stay due: nothing advanced, they fire on the first tick after the hold
            return _tick_admitted(verbose, adapters, loop, sync, can_dispatch=can_dispatch,
                                  headless=headless)


_drain_deferred_logged: set = set()


def drain_held() -> bool:
    """True while hermes-sync's core-update drain holds new kanban claims (``<home>/core-update/drain-hold``,
    the signal the kanban dispatcher reads). [daveey]

    A cron run that starts mid-drain keeps the drain busy: hermes-hub 2026-10-30 16:19-16:53 UTC, four runs
    started during the drain and the update landed after 34.1 min against a 30 min target
    (t_01a18dbde9f8b2f0). While the hold stands the tick dispatches nothing and names each due job once;
    recurring jobs keep their slot (catch-up runs them once after the hold, however many slots passed) and
    one-shots are covered by the planned pause. A hold whose writer died does not count, and a read error
    fails open: cron must not stop because the hold file is unreadable.
    """
    try:
        from agent.core_update_drain import check_drain_hold
        held = check_drain_hold("cron", logger)
    except Exception as exc:  # fail open
        logger.debug("Cron drain-hold check failed (%s); dispatching", exc, exc_info=True)
        return False
    if not held:
        if _drain_deferred_logged:
            logger.info("cron dispatch resumed: core update drain hold released; deferred jobs run now: %s",
                        ", ".join(sorted(_drain_deferred_logged)))
            _drain_deferred_logged.clear()
        return False
    _log_drain_deferred_jobs()
    return True


def _log_drain_deferred_jobs() -> None:
    """Name each job that is due while the drain hold stands, once per hold. Read-only (no advance, no claim)."""
    try:
        from cron import jobs as _jobs
        now = _jobs._hermes_now()
        for job in _jobs.load_jobs():
            job_id = job.get("id")
            if not job_id or job_id in _drain_deferred_logged or not job.get("enabled", True):
                continue
            if _jobs._has_pause_marker(job) or not job.get("next_run_at"):
                continue
            due = _jobs._ensure_aware(datetime.fromisoformat(job["next_run_at"]))
            if due > now:
                continue
            _drain_deferred_logged.add(job_id)
            logger.info("cron job %s (%s) due at %s deferred by the core-update drain hold; it runs once after "
                        "the hold", job_id, job.get("name") or "?", job["next_run_at"])
    except Exception as exc:  # naming is diagnostics only
        logger.debug("Could not list cron jobs deferred by the drain hold: %s", exc, exc_info=True)


_update_hold_logged: set = set()


@contextlib.contextmanager
def update_admission():
    """The cross-process core-update admission reservation for one tick's scan/advance/submit. [daveey]

    hermes-sync's drain window sets the admission hold, waits for reservations to reach zero, then
    publishes the update intent (the gateway's SIGUSR1). Cron used only the process fence, so a job due
    inside the window started under the hold, held the gateway's restart open past the window, and the
    update gave up while the restart still went ahead on the old code (David-MBP 2026-10-29 15:29-15:47,
    t_01a189db034f3b2b). While the hold stands this yields False and the tick dispatches nothing; runs
    registered inside it take their own reservation (``scheduler._take_run_reservation``) before this
    one is released. The reservation waits out a busy admission.lock for up to
    ``backend_retirement.BUSY_WAIT_S`` (a hub Kanban claim holds it up to CLAIM_MAX_HOLD_S; the tick holds no
    session lock, and it does not register as a waiting turn, so claims don't defer to cron). A lock still
    busy after that yields False like the hold: dispatching would run with no reservation, invisible to the
    drain (N3, t_01a1a375eb4f4c4c); due jobs fire once on a later tick. Any other reservation error
    (unwritable root) fails open: cron must not stop because the fence file is unavailable, and the drainer
    reads such a root as busy anyway.
    """
    # Preserve the updater's legacy availability policy, but never retain a
    # host-process exemption when it cannot prove lease coverage.
    from hermes_cli.worker_drain_capability import revoke
    try:
        from hermes_cli import admission_fence
        from hermes_cli.admission_fence import AdmissionBusy, SUPPORTED
        from hermes_cli.backend_retirement import BUSY_WAIT_S
        from hermes_cli.worker_drain_fence import DrainRefused, lease_scope
    except ImportError:
        revoke('cron admission import unavailable')
        yield True
        return
    if not SUPPORTED:
        revoke('cron admission unsupported')
        yield True
        return
    from cron import scheduler as _sched
    from hermes_constants import get_default_hermes_root

    pending = None
    root = None
    try:
        root = get_default_hermes_root(home=_sched._get_hermes_home())
        pending = admission_fence.reserve(root, timeout=BUSY_WAIT_S)
        if pending is not None:
            getattr(pending, "describe", lambda *_: None)("cron_tick")
    except DrainRefused:
        yield False  # leave due-time untouched; never inherit updater fail-open
        return
    except AdmissionBusy:  # before OSError: AdmissionBusy is a TimeoutError, i.e. an OSError
        logger.info("cron dispatch deferred: core update admission lock still busy after %.1fs under %s; "
                    "due jobs fire on a later tick", BUSY_WAIT_S, root)
        yield False
        return
    except (OSError, TimeoutError) as exc:
        revoke('cron admission unavailable')
        logger.debug("Cron update admission unavailable (%s); dispatching", exc)
        yield True
        return
    key = str(root)
    if pending is None:
        if key not in _update_hold_logged:
            _update_hold_logged.add(key)
            logger.info("cron dispatch held for a core update (admission hold under %s); running jobs continue, "
                        "due jobs fire after the hold", root)
        yield False
        return
    if key in _update_hold_logged:
        _update_hold_logged.discard(key)
        logger.info("cron dispatch resumed: core update admission hold released under %s", root)
    try:
        with lease_scope(getattr(pending, '_worker_drain_lease', None)):
            yield True
    finally:
        pending.close()


def _acquire_tick_lock_or_degrade(_sched):
    """The tick lock fd, or None when this tick must not run: another ticker holds the lock, or
    the store cannot be written (read-only/full/denied: the lock file cannot even be created), in
    which case the store is marked degraded and the MCP orphan sweep still runs."""
    lock_dir, lock_file = _sched._get_lock_paths()
    _sched._ensure_cron_dir(lock_dir)
    try:
        return _sched._acquire_tick_lock(lock_file)
    except OSError as exc:
        if exc.errno not in store_health.UNWRITABLE_ERRNOS:
            raise  # EMFILE/ENFILE etc. stay a FAILED tick (#87644)
        store_health.note_unwritable(exc, "tick lock unavailable", "lock", cron_dir=lock_dir)
        _sched._sweep_mcp_orphans()
        return None


def _advance_or_drop_recurring(_sched, due_jobs: list) -> list:
    """Persist the recurring jobs' advance before any runs; when the store refuses it, drop them
    (no durable advance -> a crash mid-run would re-fire them, so skipping is the at-most-once
    side). One-shots still go through their own fire claim."""
    try:
        _sched.advance_next_runs([job["id"] for job in due_jobs])
        return due_jobs
    except OSError as exc:
        skipped = [j for j in due_jobs if is_recurring(j)]
        store_health.note_unwritable(exc, f"skipped {len(skipped)} recurring job(s)", "advance", skipped)
        return [j for j in due_jobs if not is_recurring(j)]


def _pre_dispatch_housekeeping(_sched, headless: bool) -> None:
    """Admission reconcile, legacy upgrade drain and dead-owner reap, run under the tick lock
    before due jobs are read."""
    from cron.scheduler_authority import reconcile_pending
    if headless:
        reconcile_pending(allow_connect=False)
    else:
        reconcile_pending()
    # Upgrade drain for the retired CLI lane's records: never let it stop due jobs below.
    try:
        from cron.bot_chat_legacy import drain_legacy_pending
        drain_legacy_pending()
    except Exception as _legacy_exc:
        _sched.logger.warning("Legacy Bot Chat pending drain failed: %s", _legacy_exc, exc_info=True)
    _sched._maybe_reap_dead_owners()


def _tick_admitted(
    verbose: bool = True, adapters=None, loop=None, sync: bool = True, *, can_dispatch=None,
    headless: bool = False):
    """Check and run all due jobs. File-locked so only one tick runs at a time (gateway ticker vs
    standalone daemon / manual tick). ``can_dispatch``: optional gate; false leaves due jobs for the
    next allowed tick. ``headless``: the tick runs outside any gateway (system crontab, ``hermes
    cron tick``) and must refuse agent jobs rather than spawn one. Returns the number of jobs
    executed (0 if another tick holds the lock)."""
    from cron import scheduler as _sched

    # Stale-code yield gate — BEFORE the lock race. A process whose checkout was updated under it
    # serves mixed sys.modules (jobs die on ImportErrors); if a fresher gateway holds the runtime
    # lock, ITS ticker dispatches. With no fresh holder (desktop-standalone) the tick proceeds.
    _skew = _sched._should_yield_tick_to_fresh_gateway()
    if _skew is not None:
        _log_tick_yield_once(f"boot={_skew[0]} disk={_skew[1]}")
        raise _sched.CronTickYielded(_skew[0], _skew[1])

    lock_fd = _acquire_tick_lock_or_degrade(_sched)
    if lock_fd is None:
        return 0

    try:
        # `hermes pause` ESTOP: skip dispatch, never touch in-flight runs; check_paused logs once.
        with contextlib.suppress(ImportError):
            from agent.estop import check_paused as _estop_check_paused
            if _estop_check_paused("cron", _sched.logger):
                return 0

        if can_dispatch is not None and not can_dispatch():
            from cron.planned_pause import begin
            begin()
            _sched.logger.debug("Cron dispatch paused while gateway drains existing work")
            return 0

        _pre_dispatch_housekeeping(_sched, headless)
        # Periodic worktree GC (6h, threaded) — the only sweep gateway-only boxes get.
        try:
            _sched._maybe_run_worktree_maintenance()
        except Exception as _wt_exc:
            _sched.logger.debug("Worktree maintenance dispatch failed: %s", _wt_exc)

        due_jobs = _sched.get_due_jobs()
        _sched._sweep_stale_inflight_for_tick(due_jobs)
        # [daveey] Every tick that would dial the gateway (headless, or the Desktop serve ticker that
        # takes over the store while an update restarts the gateway) holds agent jobs until it is
        # ready; only the gateway's own ticker admits in-process and skips the probe.
        from cron import scheduler_gateway_gate as _gw_gate
        if due_jobs and _gw_gate.tick_needs_gate(headless):
            due_jobs = _gw_gate.refuse_agent_jobs_without_gateway(due_jobs, may_start=not headless)

        if not due_jobs:
            # Idle tick: skip config load + pool setup, but still reap crashed jobs' MCP orphans.
            if verbose:
                # Idle tick: skip config load + pool partitioning entirely (#33612 — the gateway ticker
                # calls tick(verbose=False) every 60s, so idle ticks previously fell through to
                # load_config()). Still run the post-tick MCP orphan sweep: main intentionally sweeps on
                # idle ticks so orphaned stdio children from crashed jobs are reaped even when nothing is
                # due.
                _sched.logger.info("%s - No jobs due", _sched._hermes_now().strftime('%H:%M:%S'))
            store_health.recheck_idle()
            _sched._sweep_mcp_orphans()
            return 0

        if store_health.dispatch_blocked(due_jobs):
            # Known-unwritable store, re-probed at most once a minute: every advance/claim would
            # fail, so skip them (the skip is recorded) and keep the tick's housekeeping alive.
            _sched._sweep_mcp_orphans()
            return 0

        if verbose:
            _sched.logger.info("%s - %s job(s) due", _sched._hermes_now().strftime('%H:%M:%S'), len(due_jobs))

        # Advance next_run_at for recurring jobs FIRST, under the lock, before any execution
        # (at-most-once). Re-advancing running jobs keeps the grace window alive; mark_job_run
        # overwrites it on completion. Composes with the claim-time advance in claim_job_for_fire.
        due_jobs = _advance_or_drop_recurring(_sched, due_jobs)
        if not due_jobs:
            _sched._sweep_mcp_orphans()
            return 0

        _max_workers = _sched._resolve_max_parallel_workers()
        if verbose:
            _sched.logger.info(
                "Running %d job(s) in parallel (max_workers=%s)",
                len(due_jobs),
                _max_workers if _max_workers else "unbounded")

        def _process_job(job: dict) -> bool:
            return _sched._process_due_job(job, adapters, loop, verbose)

        # Persistent pool, non-blocking dispatch. Already-running jobs are skipped; mark_job_run
        # re-arms next_run_at on completion, so no catch-up queue is needed.
        _results: list = []
        _all_futures: list = []
        pool = _sched._get_parallel_pool(_max_workers)
        for job in due_jobs:
            fut = _sched._submit_with_guard(job, pool, _process_job)
            if fut is None:
                continue
            _all_futures.append(fut)
            if not sync:
                _results.append(True)  # optimistically counted

        if sync:
            for f in concurrent.futures.as_completed(_all_futures):
                try:
                    _results.append(f.result())
                except Exception as exc:
                    _sched.logger.error("Cron job future failed: %s", exc)
                    _results.append(False)
            _sched._sweep_mcp_orphans()
            return sum(_results)

        _sched._sweep_mcp_orphans_when_all_done(_all_futures)
        return sum(_results)
    finally:
        _sched._release_tick_lock(lock_fd)
