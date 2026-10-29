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
