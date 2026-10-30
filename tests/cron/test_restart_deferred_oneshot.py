"""Planned runtime pauses preserve occurrences, not arbitrary stale schedules."""
from datetime import UTC, datetime, timedelta

import pytest

from tests.cron.test_update_admission_hold import _quiet, _root


@pytest.mark.parametrize("gate", ["drain", "admission"])
def test_oneshot_due_during_long_pause_survives_successor(tmp_path, monkeypatch, gate):
    from cron import jobs, scheduler
    from hermes_cli import admission_fence

    _quiet(monkeypatch)
    now = datetime.now(UTC).replace(microsecond=0)
    clock = [now]
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock[0])
    due = now + timedelta(seconds=70)
    job = jobs.create_job(prompt="run once", schedule=due.isoformat())
    stale = jobs.create_job(prompt="already missed", schedule=now.isoformat())
    jobs.update_job(stale["id"], {"next_run_at": (now - timedelta(seconds=121)).isoformat()})
    future = jobs.create_job(prompt="later", schedule=(now + timedelta(hours=1)).isoformat())
    root = _root()
    if gate == "admission":
        admission_fence.set_hold(root)
    try:
        assert scheduler.tick(verbose=False, can_dispatch=lambda: False) == 0
        kept = jobs.get_job(job["id"])
        assert kept and kept["next_run_at"] == due.isoformat()
    finally:
        if gate == "admission":
            admission_fence.clear_hold(root)
    # A successor has no memory of the ticker that was held. Its only evidence is on disk.
    clock[0] = now + timedelta(seconds=240)
    selected = jobs.get_due_jobs()
    assert [j["id"] for j in selected] == [job["id"]]
    assert jobs.get_due_jobs() == []  # a second owner cannot take the live claim
    jobs.mark_job_run(job["id"], success=True)
    assert jobs.get_due_jobs() == []
    assert jobs.get_job(stale["id"]) is None
    # Future slots do not gain permanent grace from an old pause.
    clock[0] = now + timedelta(hours=2)
    assert jobs.get_due_jobs() == []
    assert jobs.get_job(future["id"]) is None


def test_refusal_and_schedule_edit_do_not_lose_or_widen_recovery(monkeypatch):
    from cron import jobs, planned_pause, scheduler_gateway_gate
    now = datetime.now(UTC).replace(microsecond=0)
    clock = [now]
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock[0])
    job = jobs.create_job(prompt="run", schedule=(now + timedelta(seconds=30)).isoformat())
    planned_pause.begin()
    clock[0] += timedelta(seconds=60)  # first scan is still inside ordinary grace
    selected = jobs.get_due_jobs()
    assert [j["id"] for j in selected] == [job["id"]]
    scheduler_gateway_gate._note_fire_refused([job["id"]], "gateway starting")
    clock[0] += timedelta(seconds=180)
    assert [j["id"] for j in jobs.get_due_jobs()] == [job["id"]]
    scheduler_gateway_gate._note_fire_refused([job["id"]], "gateway starting")
    # A new instant cannot inherit the old occurrence's exemption.
    jobs.update_job(job["id"], {"next_run_at": (clock[0] - timedelta(seconds=180)).isoformat()})
    assert jobs.get_due_jobs() == []
    assert jobs.get_job(job["id"]) is None


def test_pause_recovery_contains_corruption_and_failed_end_write(monkeypatch):
    import errno
    from cron import jobs, planned_pause
    now = datetime.now(UTC).replace(microsecond=0)
    clock = [now]
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock[0])
    job = jobs.create_job(prompt="run", schedule=(now + timedelta(seconds=30)).isoformat())
    planned_pause.begin()
    clock[0] += timedelta(seconds=240)
    with monkeypatch.context() as m:
        m.setattr(planned_pause, "_write", lambda *_: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full")))
        assert jobs.get_due_jobs() == []
        assert jobs.get_job(job["id"]) is not None
    assert [j["id"] for j in jobs.get_due_jobs()] == [job["id"]]
    assert not list(planned_pause._path().glob("*.json"))
    jobs.mark_job_run(job["id"], success=True)
    # A corrupt sidecar must not stop unrelated due work.
    next_job = jobs.create_job(prompt="healthy", schedule=clock[0].isoformat())
    (planned_pause._path() / "bad.json").write_text('{"bad": true}')
    assert [j["id"] for j in jobs.get_due_jobs()] == [next_job["id"]]


def test_pause_publication_never_waits_on_jobs_lock_and_cleanup_is_snapshot_local():
    from concurrent.futures import ThreadPoolExecutor
    from cron import jobs, planned_pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        with jobs._jobs_lock():
            pool.submit(planned_pause.begin).result(timeout=5)
    recovery = planned_pause.recover([], datetime.now(UTC))
    assert recovery and len(recovery[1]) == 1
    planned_pause.begin()  # another ticker publishes after the scan snapshot
    planned_pause.finish_recovery(recovery[1])
    remaining = list(planned_pause._path().glob("*.json"))
    assert len(remaining) == 1
    assert remaining[0] not in recovery[1]
