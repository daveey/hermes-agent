"""Cron defers newly due jobs while hermes-sync's core-update drain holds new claims (t_01a18dbde9f8b2f0).

hermes-hub 2026-10-30 16:19-16:53 UTC: the drain held new kanban claims through
``<home>/core-update/drain-hold`` but cron kept dispatching. Three runs that started mid-drain
(cron:1663d7c8440f, cron:6043b9450f3e, cron:coach:eae6027397cb, plus x-reply-discovery at 16:37) kept the
drain busy, so the update applied after 34.1 min against a 30 min target. The ticker now honours the same
hold the kanban dispatcher reads (``agent.core_update_drain``): due jobs stay due, are named once in the
log, and run once on the first tick after the hold (the restarted gateway's ticker).
"""

import json
import logging
import os
from datetime import UTC, datetime, timedelta


def _quiet(monkeypatch):
    from cron import scheduler

    for name in ("_maybe_run_worktree_maintenance", "_sweep_mcp_orphans", "_maybe_reap_dead_owners"):
        monkeypatch.setattr(scheduler, name, lambda: None)
    monkeypatch.setattr(scheduler, "_should_yield_tick_to_fresh_gateway", lambda: None)


def _set_drain_hold(pid=None):
    from agent.core_update_drain import hold_paths

    path = hold_paths()[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid or os.getpid(), "started_at": 0, "reason": "hermes-sync core update drain"}))
    return path


def _job(tmp_path, schedule="every 15m", due=None):
    from cron import jobs

    script = tmp_path / "job.py"
    script.write_text("print('x')\n")
    job = jobs.create_job(prompt=None, schedule=schedule, script=str(script), no_agent=True, name="nightly-digest")
    jobs.update_job(job["id"], {"next_run_at": (due or datetime.now(UTC)).isoformat()})
    return job


def test_a_job_due_during_the_drain_does_not_start(tmp_path, monkeypatch, caplog):
    """(a) While the drain hold stands the tick starts nothing and leaves the job due (nothing advanced)."""
    from cron import jobs, scheduler

    _quiet(monkeypatch)
    ran = []
    monkeypatch.setattr(scheduler, "_process_due_job", lambda job, *a: ran.append(job["id"]) or True)
    job = _job(tmp_path)
    before = jobs.get_job(job["id"])["next_run_at"]
    hold = _set_drain_hold()
    try:
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            for _ in range(3):
                assert scheduler.tick(verbose=False) == 0
        assert ran == []
        assert jobs.get_job(job["id"])["next_run_at"] == before, "a deferred job is not advanced or skipped"
        named = [r.getMessage() for r in caplog.records if "deferred by the core-update drain hold" in r.getMessage()]
        assert len(named) == 1, f"the deferred job is named once per hold, not per tick: {named}"
        assert job["id"] in named[0] and "nightly-digest" in named[0]
    finally:
        hold.unlink()


def test_deferred_job_runs_exactly_once_after_the_hold(tmp_path, monkeypatch):
    """(c) A job that missed several slots while held runs once on the first tick after the hold ends (the
    restarted gateway's first tick), then is not due again until its next slot."""
    from cron import jobs, scheduler

    _quiet(monkeypatch)
    ran = []

    def fire(job, *a):  # the real fire claim (it advances the slot); only the run itself is stubbed
        assert jobs.claim_job_for_fire(job["id"])
        ran.append(job["id"])
        jobs.mark_job_run(job["id"], True)
        return True

    monkeypatch.setattr(scheduler, "_process_due_job", fire)
    # Due 40 min ago: the hold spanned the slot and two more; only one catch-up run, never three.
    job = _job(tmp_path, due=datetime.now(UTC) - timedelta(minutes=40))
    hold = _set_drain_hold()
    try:
        assert scheduler.tick(verbose=False) == 0
    finally:
        hold.unlink()
    assert scheduler.tick(verbose=False) == 1
    assert scheduler.tick(verbose=False) == 0
    assert scheduler.tick(verbose=False) == 0
    assert ran == [job["id"]]
    nxt = datetime.fromisoformat(jobs.get_job(job["id"])["next_run_at"])
    assert nxt > datetime.now(UTC), "re-armed for a future slot after the one catch-up run"


def test_a_dead_drainer_does_not_freeze_cron(tmp_path, monkeypatch):
    """The hold only counts while its writer lives (as for the dispatcher): a crashed drainer never stops cron."""
    import subprocess
    import sys
    from cron import scheduler

    _quiet(monkeypatch)
    ran = []
    monkeypatch.setattr(scheduler, "_process_due_job", lambda job, *a: ran.append(job["id"]) or True)
    job = _job(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    hold = _set_drain_hold(pid=dead.pid)
    try:
        assert scheduler.tick(verbose=False) == 1
        assert ran == [job["id"]]
    finally:
        hold.unlink()


def test_a_deferred_one_shot_survives_the_hold(tmp_path, monkeypatch):
    """A one-shot due inside the hold is recorded as a planned pause, so it fires after the hold instead of being
    retired as missed (same path as the admission hold)."""
    import time
    from cron import jobs, scheduler

    _quiet(monkeypatch)
    monkeypatch.setattr(jobs, "ONESHOT_GRACE_SECONDS", 1)
    ran = []
    monkeypatch.setattr(scheduler, "_process_due_job", lambda job, *a: ran.append(job["id"]) or True)
    script = tmp_path / "once.py"
    script.write_text("print('x')\n")
    job = jobs.create_job(prompt=None, schedule=(datetime.now(UTC) + timedelta(seconds=2)).isoformat(),
                          script=str(script), no_agent=True)
    hold = _set_drain_hold()
    try:
        assert scheduler.tick(verbose=False) == 0
        time.sleep(4)  # health: allow HX013 -- the slot must pass inside the hold and outlive its (1 s) grace
        assert scheduler.tick(verbose=False) == 0
    finally:
        hold.unlink()
    assert scheduler.tick(verbose=False) == 1
    assert ran == [job["id"]]
