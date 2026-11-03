"""Cron honours the core-update admission fence (t_01a189db034f3b2b).

hermes-sync's drain window sets ``<root>/core-update/holds/hermes-sync`` and publishes the update intent
only once every admission reservation is gone. Cron took no reservation, so a job due in the window
started under the hold and held the gateway restart open past the window (David-MBP 2026-10-29).
"""

from datetime import UTC, datetime
import threading


def _root():
    from cron import scheduler
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root(home=scheduler._get_hermes_home())


def _quiet(monkeypatch):
    from cron import scheduler

    for name in ("_maybe_run_worktree_maintenance", "_sweep_mcp_orphans", "_maybe_reap_dead_owners"):
        monkeypatch.setattr(scheduler, name, lambda: None)
    monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)


def test_a_job_due_during_the_hold_is_deferred_not_dropped(tmp_path, monkeypatch):
    from cron import jobs, scheduler
    from hermes_cli import admission_fence

    _quiet(monkeypatch)
    ran = []
    monkeypatch.setattr(scheduler, "_process_due_job", lambda job, *a: ran.append(job["id"]) or True)
    script = tmp_path / "job.py"
    script.write_text("print('x')\n")
    job = jobs.create_job(prompt=None, schedule="every 15m", script=str(script), no_agent=True)
    jobs.update_job(job["id"], {"next_run_at": datetime.now(UTC).isoformat()})
    before = jobs.get_job(job["id"])["next_run_at"]

    root = _root()
    admission_fence.set_hold(root)
    try:
        assert scheduler.tick(verbose=False) == 0
        assert ran == []
        assert jobs.get_job(job["id"])["next_run_at"] == before  # still due, not advanced
    finally:
        admission_fence.clear_hold(root)
    assert scheduler.tick(verbose=False) == 1
    assert ran == [job["id"]]


def test_a_running_job_holds_an_admission_reservation_until_it_ends(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from cron import jobs, scheduler
    from hermes_cli import admission_fence

    _quiet(monkeypatch)
    running, release = threading.Event(), threading.Event()

    def process_job(*args):
        running.set()
        assert release.wait(10)
        return True

    monkeypatch.setattr(scheduler, "_process_due_job", process_job)
    script = tmp_path / "job.py"
    script.write_text("print('x')\n")
    job = jobs.create_job(prompt=None, schedule=datetime.now(UTC).isoformat(), script=str(script), no_agent=True)
    root = _root()
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda *a: pool)
        try:
            assert scheduler.tick(verbose=False, sync=False) == 1
            assert running.wait(10)
            assert job["id"] in scheduler.get_running_job_ids()
            # The tick's own reservation is gone; the run's stays until it ends.
            assert admission_fence.state(root)["reservations"] == 1
            # A hold set mid-run does not hide it from the updater's post-hold read.
            admission_fence.set_hold(root)
            assert admission_fence.state(root) == {"version": 1, "held": True, "reservations": 1}
        finally:
            release.set()
    deadline = threading.Event()
    for _ in range(100):
        if not scheduler.get_running_job_ids():
            break
        deadline.wait(0.05)
    try:
        assert admission_fence.state(root)["reservations"] == 0
    finally:
        admission_fence.clear_hold(root)


def test_a_scope_isolated_run_drops_its_reservation_an_in_process_run_keeps_it(tmp_path, monkeypatch):
    """A scoped external worker survives the gateway restart, which excludes it from its wait; the
    update drain must not wait on it either (hub: 4 restart-safe scoped crons at once)."""
    from cron import scheduler
    from hermes_cli import admission_fence

    root = _root()
    assert scheduler.try_register_running_job("scoped-job")
    assert scheduler.try_register_running_job("inproc-job")
    try:
        assert admission_fence.state(root)["reservations"] == 2
        scheduler._record_external_cron_worker("scoped-job", 4242, scope_isolated=True)
        scheduler._record_external_cron_worker("inproc-job", 4243, scope_isolated=False)
        assert admission_fence.state(root)["reservations"] == 1
        assert "scoped-job" in scheduler.get_running_job_ids()  # still in flight for dedupe
    finally:
        scheduler.release_running_job("scoped-job")
        scheduler.release_running_job("inproc-job")
    assert admission_fence.state(root)["reservations"] == 0



# [daveey] N3 (delta review t_01a1a3679692b3c2): with the hub CPU gate on, a Kanban claim holds admission.lock for
# ~1-2.25 s (CLAIM_MAX_HOLD_S). The tick's reservation used one 50 ms try and, as AdmissionBusy is a TimeoutError,
# failed OPEN: it dispatched with no reservation and never checked the hold, so the run was invisible to the drain.


def _timed_lock_child(root, ready, hold_s):
    import time
    from hermes_cli import admission_fence

    with admission_fence.admission_lock(root, timeout=5):
        ready.set()
        time.sleep(hold_s)  # health: allow HX013 -- how long another process holds the lock IS the contention under test


def _spawn_lock_holder(root, hold_s):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    child = ctx.Process(target=_timed_lock_child, args=(str(root), ready, hold_s))
    child.start()
    assert ready.wait(30)
    return child


def _reap(child):
    if child.is_alive():
        child.kill()
    child.join(10)


def _due_job(tmp_path):
    from cron import jobs

    script = tmp_path / "job.py"
    script.write_text("print('x')\n")
    job = jobs.create_job(prompt=None, schedule="every 15m", script=str(script), no_agent=True)
    jobs.update_job(job["id"], {"next_run_at": datetime.now(UTC).isoformat()})
    return job["id"]


def test_tick_waits_out_a_claim_holding_the_lock_and_dispatches_under_a_reservation(tmp_path, monkeypatch):
    """A claim holding admission.lock 1.5 s (inside CLAIM_MAX_HOLD_S): the tick waits, then reserves; the run is
    visible to the updater while it dispatches, and the wait registers no turn-waiter marker (cron must not make
    claims defer)."""
    import time
    from cron import scheduler
    from hermes_cli import admission_fence
    from hermes_cli.backend_retirement import BUSY_WAIT_S
    from tests.waiting import wait_until

    assert BUSY_WAIT_S > 1.5
    _quiet(monkeypatch)
    root = _root()
    seen = []
    monkeypatch.setattr(scheduler, "_process_due_job",
                        lambda job, *a: seen.append((job["id"], admission_fence.state(root)["reservations"])) or True)
    job_id = _due_job(tmp_path)

    holder = _spawn_lock_holder(root, 1.5)
    result, waiter_seen = {}, []
    try:
        started = time.monotonic()
        ticker = threading.Thread(target=lambda: result.setdefault("n", scheduler.tick(verbose=False)))
        ticker.start()

        def tick_done():  # sample the turn-waiter markers all through the tick's wait
            waiter_seen.append(admission_fence.turns_waiting(root / "core-update"))
            return not ticker.is_alive()

        wait_until(tick_done, timeout=30, interval=0.05, what="the tick")
        ticker.join(30)
        waited = time.monotonic() - started
    finally:
        _reap(holder)
    assert result.get("n") == 1
    assert [j for j, _n in seen] == [job_id]
    assert seen[0][1] >= 1, "the run dispatched with no admission reservation (fail-open)"
    assert waited >= 0.5, "the tick did not wait for the busy lock"
    assert waiter_seen and not any(waiter_seen), "cron registered as a waiting turn"
    assert admission_fence.state(root)["reservations"] == 0


def test_tick_defers_when_the_lock_stays_busy_past_the_wait_budget(tmp_path, monkeypatch):
    """Still busy after BUSY_WAIT_S: the tick dispatches nothing and advances nothing (fail closed, no
    reservation-less run); after the lock frees the job fires exactly once."""
    from cron import jobs, scheduler
    from hermes_cli import admission_fence
    from hermes_cli.backend_retirement import BUSY_WAIT_S

    _quiet(monkeypatch)
    root = _root()
    ran = []
    monkeypatch.setattr(scheduler, "_process_due_job", lambda job, *a: ran.append(job["id"]) or True)
    job_id = _due_job(tmp_path)
    before = jobs.get_job(job_id)["next_run_at"]

    holder = _spawn_lock_holder(root, BUSY_WAIT_S + 2.0)
    try:
        assert scheduler.tick(verbose=False) == 0
        assert holder.is_alive(), "the hold ended before the tick gave up; the case is not exercised"
        assert ran == []
        assert jobs.get_job(job_id)["next_run_at"] == before  # still due, not advanced
    finally:
        _reap(holder)
    assert admission_fence.state(root)["reservations"] == 0
    # The first tick after the lock frees fires it once (the stub never marks the run done, so a further tick
    # here would re-see it as due; that is the harness, not the fence).
    assert scheduler.tick(verbose=False) == 1
    assert ran == [job_id]
    assert jobs.get_job(job_id)["next_run_at"] > before  # advanced by that one dispatch


def test_run_reservation_waits_out_a_claim_holding_the_lock(monkeypatch):
    """A run registered while a claim holds the lock 1.5 s still takes its reservation (``extend``)."""
    from cron import scheduler
    from hermes_cli import admission_fence

    root = _root()
    holder = _spawn_lock_holder(root, 1.5)
    try:
        assert scheduler.try_register_running_job("busy-lock-job")
    finally:
        _reap(holder)
    try:
        assert admission_fence.state(root)["reservations"] == 1
    finally:
        scheduler.release_running_job("busy-lock-job")
    assert admission_fence.state(root)["reservations"] == 0


def test_an_unwritable_fence_root_still_fails_open(tmp_path, monkeypatch):
    """Unchanged: an OSError other than a busy lock (unwritable root) dispatches; the drainer reads such a root as
    busy anyway."""
    from cron import scheduler
    from hermes_cli import admission_fence

    _quiet(monkeypatch)
    ran = []
    monkeypatch.setattr(scheduler, "_process_due_job", lambda job, *a: ran.append(job["id"]) or True)
    job_id = _due_job(tmp_path)

    def unwritable(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(admission_fence, "reserve", unwritable)
    monkeypatch.setattr(admission_fence, "extend", unwritable)
    assert scheduler.tick(verbose=False) == 1
    assert ran == [job_id]
