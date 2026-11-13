"""A cron attempt cut off by its owner process's exit (gateway restart/update) reruns once, at once.

Each test drives the real sequence: the tick/fire claim, the ledger row of the in-flight attempt,
the owner's death, then the next process's ``recover_interrupted_executions``.
"""
from datetime import datetime, timedelta
import logging

import pytest

from cron import executions
from cron.jobs import (
    claim_dispatch,
    claim_job_for_fire,
    create_job,
    get_due_jobs,
    get_job,
    mark_job_run,
    pause_job,
    remove_job,
    update_job,
)

RESCHEDULED = "after restart interruption"


@pytest.fixture()
def clock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    monkeypatch.setattr("cron.executions._end_interrupted_sessions", lambda _records: None)
    state = {"now": datetime.fromisoformat("2026-10-28T09:00:00-07:00")}
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: state["now"])
    monkeypatch.setattr("cron.executions._hermes_now", lambda: state["now"])
    # A rerun is held while the host is not fully awake (hold_for_wake): read awake here, so a
    # test run on a MacBook on battery is no DarkWake. ``awake_host`` drives it per test.
    monkeypatch.setattr("cron.restart_rerun.host_fully_awake", lambda: True)
    return state


def _start(job_id, **claim_kwargs):
    """A fire in flight: claimed, dispatched and running in the (soon dead) owner process."""
    claimed = claim_job_for_fire(job_id, return_job=True, **claim_kwargs)
    assert isinstance(claimed, dict), "fire was not claimed"
    assert claim_dispatch(job_id)
    record = executions.create_execution(job_id, source="builtin")
    assert executions.mark_execution_running(record["id"])
    return claimed, record


def _restart(monkeypatch, owner_alive=False, scheduler_alive=None):
    """The owner exits; the replacement process recovers the ledger at startup. The claiming
    scheduler (this test process stamps the fire claim) dies with it unless ``scheduler_alive``."""
    _kill_owner(monkeypatch, owner_alive, scheduler_alive)
    with executions.startup_recovery():
        return executions.recover_interrupted_executions()


def _kill_owner(monkeypatch, owner_alive=False, scheduler_alive=None):
    import os

    scheduler_alive = owner_alive if scheduler_alive is None else scheduler_alive
    monkeypatch.setattr(executions, "_PROCESS_ID", f"replacement-{datetime.now().timestamp()}")
    monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: owner_alive)
    monkeypatch.setattr("gateway.status._pid_exists",
                        lambda pid: scheduler_alive or pid != os.getpid())


def _finish(job_id):
    claimed = claim_job_for_fire(job_id, return_job=True)
    assert isinstance(claimed, dict), "rerun was not claimable"
    assert claim_dispatch(job_id)
    assert mark_job_run(job_id, success=True, expected_fire_owner=claimed["fire_claim"]["by"])


def _due_ids():
    return [job["id"] for job in get_due_jobs()]


def test_recurring_job_reruns_once_and_keeps_its_schedule(clock, monkeypatch, caplog):
    job = create_job("hourly report", "every 60m", name="report")
    clock["now"] += timedelta(minutes=61)
    assert _due_ids() == [job["id"]]
    _claimed, record = _start(job["id"])
    natural_next = get_job(job["id"])["next_run_at"]
    clock["now"] += timedelta(minutes=5)

    with caplog.at_level(logging.INFO, logger="cron.jobs"):
        assert _restart(monkeypatch) == 1

    assert f"cron: rescheduled {job['id']} (execution {record['id']}) {RESCHEDULED}" in caplog.text
    after = get_job(job["id"])
    assert after["next_run_at"] == clock["now"].isoformat()
    assert int(after.get("failure_streak") or 0) == 0
    assert after.get("last_status") is None and after.get("last_failure") is None
    assert _due_ids() == [job["id"]]

    # A second recovery pass in another process finds nothing to reschedule.
    assert _restart(monkeypatch) == 0
    assert get_job(job["id"])["next_run_at"] == clock["now"].isoformat()

    _finish(job["id"])
    done = get_job(job["id"])
    assert done["next_run_at"] == natural_next  # normal schedule kept
    assert done["last_status"] == "ok" and done.get("restart_rerun") is None
    assert _due_ids() == []


def test_rerun_cut_off_again_is_not_rescheduled(clock, monkeypatch, caplog):
    """A job whose run kills the gateway every time must not loop."""
    job = create_job("crashy", "every 60m", name="crashy")
    clock["now"] += timedelta(minutes=61)
    assert _due_ids() == [job["id"]]
    _start(job["id"])
    assert _restart(monkeypatch) == 1
    assert _due_ids() == [job["id"]]
    clock["now"] += timedelta(minutes=2)
    _claimed, rerun = _start(job["id"])
    natural_next = get_job(job["id"])["next_run_at"]

    with caplog.at_level(logging.INFO, logger="cron.jobs"):
        assert _restart(monkeypatch) == 1

    assert f"cron: rescheduled {job['id']} (execution {rerun['id']})" not in caplog.text
    assert get_job(job["id"])["next_run_at"] == natural_next
    assert _due_ids() == []


@pytest.mark.parametrize("settled", [False, True])
def test_deferral_inherits_only_a_pending_rerun_marker(settled):
    from cron.restart_rerun import _marker

    old = "2026-10-28T09:00:00-07:00"
    now = "2026-10-28T10:00:00-07:00"
    job = {"restart_rerun": {"execution_id": "old", "at": old, "settled": settled}}
    marker = _marker(job, {"id": "deferred"}, now, deferred=True)
    assert marker == ({"execution_id": "deferred", "at": now, "deferred": True} if settled
                      else {"execution_id": "deferred", "at": old, "fire_at": now})


def test_skipped_rerun_expires_guard_but_still_rejects_replay(clock, monkeypatch):
    from cron.restart_rerun import reschedule_interrupted

    job = create_job("crashy", "every 60m", name="crashy")
    clock["now"] += timedelta(minutes=61)
    _start(job["id"])
    assert _restart(monkeypatch) == 1
    clock["now"] += timedelta(minutes=1)
    _claimed, rerun = _start(job["id"])
    assert _restart(monkeypatch) == 1
    saved = get_job(job["id"])
    assert not reschedule_interrupted(rerun)
    assert get_job(job["id"]) == saved

    clock["now"] = datetime.fromisoformat(saved["next_run_at"])
    _start(job["id"])
    assert _restart(monkeypatch) == 1
    assert _due_ids() == [job["id"]], "a later natural execution gets its own rerun"


def test_paused_deleted_or_edited_jobs_do_not_rerun(clock, monkeypatch, caplog):
    paused = create_job("a", "every 60m", name="paused")
    deleted = create_job("b", "every 60m", name="deleted")
    edited = create_job("c", "every 60m", name="edited")
    clock["now"] += timedelta(minutes=61)
    assert sorted(_due_ids()) == sorted([paused["id"], deleted["id"], edited["id"]])
    for job in (paused, deleted, edited):
        _start(job["id"])
    clock["now"] += timedelta(minutes=1)
    pause_job(paused["id"])
    assert remove_job(deleted["id"])
    update_job(edited["id"], {"prompt": "c, but different"})
    edited_next = get_job(edited["id"])["next_run_at"]

    with caplog.at_level(logging.INFO, logger="cron.jobs"):
        assert _restart(monkeypatch) == 3

    assert "cron: rescheduled" not in caplog.text
    assert "fence was busy" not in caplog.text  # each skip names its own reason
    assert get_job(paused["id"])["state"] == "paused"
    assert get_job(deleted["id"]) is None
    assert get_job(edited["id"])["next_run_at"] == edited_next
    assert _due_ids() == []


def test_oneshot_cut_off_by_restart_runs_again_once(clock, monkeypatch):
    job = create_job("remind me", "in 30m", name="reminder")
    clock["now"] += timedelta(minutes=30, seconds=5)
    assert _due_ids() == [job["id"]]
    _start(job["id"])
    assert get_job(job["id"])["repeat"]["completed"] == 1
    clock["now"] += timedelta(minutes=10)

    assert _restart(monkeypatch) == 1

    after = get_job(job["id"])
    assert (after["state"], after["enabled"]) == ("scheduled", True)
    assert after["next_run_at"] == clock["now"].isoformat()
    assert after["repeat"]["completed"] == 0
    assert _due_ids() == [job["id"]]
    _finish(job["id"])
    done = get_job(job["id"])
    assert (done["state"], done["next_run_at"]) == ("completed", None)
    assert done["repeat"] == {"times": 1, "completed": 1}
    assert _restart(monkeypatch) == 0
    assert _due_ids() == []


def test_oneshot_retired_by_the_shutdown_mark_is_rearmed(clock, monkeypatch):
    """Gateway shutdown books the in-flight one-shot interrupted (completing it) and exits before
    the run's ledger row is final: the rerun must still happen, with no failure counted."""
    job = create_job("remind me", "in 30m", name="reminder")
    clock["now"] += timedelta(minutes=30, seconds=5)
    assert _due_ids() == [job["id"]]
    claimed, _record = _start(job["id"])
    _book_stop(job["id"], claimed)
    assert get_job(job["id"])["state"] == "completed"

    assert _restart(monkeypatch) == 1

    after = get_job(job["id"])
    assert (after["state"], after["next_run_at"]) == ("scheduled", clock["now"].isoformat())
    assert int(after.get("failure_streak") or 0) == 0
    assert _due_ids() == [job["id"]]


def test_manual_run_of_a_oneshot_reruns_as_a_manual_run(clock, monkeypatch):
    """A run-now cut off by a restart reruns as a run-now: the real occurrence is not consumed."""
    job = create_job("remind me", "in 30m", name="reminder")
    slot = job["next_run_at"]
    _start(job["id"], manual=True)

    assert _restart(monkeypatch) == 1

    assert _due_ids() == [job["id"]]
    _finish(job["id"])
    after = get_job(job["id"])
    assert (after["state"], after["next_run_at"]) == ("scheduled", slot)
    assert after["repeat"] == {"times": 1, "completed": 0}


def test_live_but_wedged_owner_is_not_rescheduled(clock, monkeypatch):
    """A restart-safe worker that outlived the gateway restart is still running, only stale:
    the scheduler that claimed the fire is gone, but a rerun would run the job twice at once."""
    job = create_job("slow", "every 60m", name="slow")
    clock["now"] += timedelta(minutes=61)
    assert _due_ids() == [job["id"]]
    _start(job["id"])
    natural_next = get_job(job["id"])["next_run_at"]
    clock["now"] += timedelta(minutes=5)
    monkeypatch.setattr(executions, "_live_owner_stale_after_seconds", lambda: 60.0)

    assert _restart(monkeypatch, owner_alive=True, scheduler_alive=False) == 1

    assert get_job(job["id"])["next_run_at"] == natural_next
    assert get_job(job["id"]).get("restart_rerun") is None


SHUTDOWN_MARK = "Gateway " + "shutdown interrupted the run"


def _book_stop(job_id, claimed):
    """What gateway stop bookkeeping writes for an in-flight run (mark_running_jobs_interrupted)."""
    assert mark_job_run(job_id, False, SHUTDOWN_MARK,
                        expected_fire_owner=claimed["fire_claim"]["by"], shutdown_interrupted=True)


def test_same_execution_is_never_rescheduled_twice(clock, monkeypatch):
    from cron.restart_rerun import reschedule_interrupted

    job = create_job("hourly report", "every 60m", name="report")
    clock["now"] += timedelta(minutes=61)
    assert _due_ids() == [job["id"]]
    _start(job["id"])
    clock["now"] += timedelta(minutes=1)
    assert _restart(monkeypatch) == 1
    record = executions.latest_execution(job["id"])
    first = get_job(job["id"])

    assert reschedule_interrupted(record) is False
    assert get_job(job["id"]) == first


def test_bookkeeping_writes_during_the_run_do_not_count_as_edits(clock, monkeypatch):
    job = create_job("hourly report", "every 60m", name="report")
    clock["now"] += timedelta(minutes=61)
    assert _due_ids() == [job["id"]]
    _start(job["id"])
    clock["now"] += timedelta(minutes=1)
    update_job(job["id"], {"last_delivery_error": "send failed"})  # finish_interrupted_run
    update_job(job["id"], {"monitor_state": {"last_output_hash": "x"}})

    assert _restart(monkeypatch) == 1

    assert get_job(job["id"])["next_run_at"] == clock["now"].isoformat()


def test_rerun_stopped_gracefully_is_not_rescheduled(clock, monkeypatch):
    """Stop bookkeeping (``shutdown_interrupted``) is no real outcome: it must not settle the
    rerun marker, or a job that keeps tripping restarts loops through the graceful path."""
    job = create_job("crashy", "every 60m", name="crashy")
    clock["now"] += timedelta(minutes=61)
    assert _due_ids() == [job["id"]]
    _start(job["id"])
    assert _restart(monkeypatch) == 1
    assert _due_ids() == [job["id"]]
    clock["now"] += timedelta(minutes=2)
    claimed, _rerun = _start(job["id"])
    _book_stop(job["id"], claimed)
    natural_next = get_job(job["id"])["next_run_at"]

    assert _restart(monkeypatch) == 1

    assert get_job(job["id"])["next_run_at"] == natural_next
    assert _due_ids() == []


def test_pause_written_without_an_edit_stamp_still_blocks_the_rerun(clock, monkeypatch):
    """A pause booked by a core from before ``edited_at`` (the update that restarts the gateway
    runs the old code until then) still stops the rerun."""
    from cron.jobs import load_jobs, save_jobs

    job = create_job("a", "every 60m", name="paused")
    clock["now"] += timedelta(minutes=61)
    assert _due_ids() == [job["id"]]
    _start(job["id"])
    jobs = load_jobs()
    jobs[0].update(enabled=False, state="paused", paused_at=clock["now"].isoformat())
    save_jobs(jobs)

    assert _restart(monkeypatch) == 1

    assert get_job(job["id"])["state"] == "paused"
    assert get_job(job["id"]).get("restart_rerun") is None


def test_recurring_job_retired_by_its_repeat_limit_is_not_revived(clock, monkeypatch):
    job = create_job("last one", "every 60m", name="limited", repeat=1)
    clock["now"] += timedelta(minutes=61)
    assert _due_ids() == [job["id"]]
    claimed, _record = _start(job["id"])
    _book_stop(job["id"], claimed)
    assert get_job(job["id"])["state"] == "completed"

    assert _restart(monkeypatch) == 1

    assert get_job(job["id"])["state"] == "completed"
    assert get_job(job["id"])["next_run_at"] is None
    assert get_job(job["id"]).get("restart_rerun") is None
    assert _due_ids() == []


def test_oneshot_completed_before_the_attempt_is_not_revived(clock, monkeypatch):
    job = create_job("remind me", "in 30m", name="reminder")
    clock["now"] += timedelta(minutes=30, seconds=5)
    assert _due_ids() == [job["id"]]
    _finish(job["id"])
    assert get_job(job["id"])["state"] == "completed"
    clock["now"] += timedelta(minutes=5)
    stray = executions.create_execution(job["id"], source="cli")  # e.g. a hand-run attempt
    assert executions.mark_execution_running(stray["id"])

    assert _restart(monkeypatch) == 1

    assert get_job(job["id"])["state"] == "completed"


def test_attempt_with_an_admission_journal_is_left_to_journal_recovery(clock, monkeypatch):
    from cron.scheduler_authority import journal_path

    job = create_job("hourly report", "every 60m", name="report")
    clock["now"] += timedelta(minutes=61)
    assert _due_ids() == [job["id"]]
    _claimed, record = _start(job["id"])
    natural_next = get_job(job["id"])["next_run_at"]
    path = journal_path(job["id"], record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")

    assert _restart(monkeypatch) == 1

    assert get_job(job["id"])["next_run_at"] == natural_next


def test_older_attempt_is_not_rerun_when_a_newer_one_exists(clock, monkeypatch):
    job = create_job("hourly report", "every 60m", name="report")
    clock["now"] += timedelta(minutes=61)
    assert _due_ids() == [job["id"]]
    _start(job["id"])
    natural_next = get_job(job["id"])["next_run_at"]
    clock["now"] += timedelta(minutes=1)
    newer = executions.create_execution(job["id"], source="builtin")
    assert executions.finish_execution(newer["id"], success=True)

    assert _restart(monkeypatch) == 1

    assert get_job(job["id"])["next_run_at"] == natural_next


def test_external_worker_death_under_a_live_scheduler_is_not_rescheduled(clock, monkeypatch):
    """The attempt's executor (an external cron worker) died, but the gateway that claimed the
    fire is alive: it settles the outcome (scheduler_worker_failure) and owns the claim."""
    job = create_job("script job", "every 60m", name="script")
    clock["now"] += timedelta(minutes=61)
    assert _due_ids() == [job["id"]]
    claimed, _record = _start(job["id"])
    natural_next = get_job(job["id"])["next_run_at"]

    assert _restart(monkeypatch, owner_alive=False, scheduler_alive=True) == 1

    after = get_job(job["id"])
    assert after["next_run_at"] == natural_next
    assert after["fire_claim"] == claimed["fire_claim"]
    assert after.get("restart_rerun") is None


def _interrupted_hourly(clock, name):
    job = create_job(name, "every 60m", name=name)
    clock["now"] += timedelta(minutes=61)
    assert job["id"] in _due_ids()
    _start(job["id"])
    clock["now"] += timedelta(minutes=1)
    return job


@pytest.mark.parametrize("operator_action", ["resume", "run_now"])
def test_resume_or_run_now_since_the_attempt_blocks_the_rerun(clock, monkeypatch, operator_action):
    from cron.jobs import resume_job, trigger_job

    job = _interrupted_hourly(clock, operator_action)
    if operator_action == "resume":
        pause_job(job["id"])
        clock["now"] += timedelta(seconds=30)
        resume_job(job["id"])
    else:
        trigger_job(job["id"])
    before = get_job(job["id"])

    assert _restart(monkeypatch) == 1

    after = get_job(job["id"])
    assert after.get("restart_rerun") is None
    assert (after["next_run_at"], after.get("manual_run_at")) == (
        before["next_run_at"], before.get("manual_run_at"))


def test_rearmed_oneshot_is_not_rerun_over_the_new_time(clock, monkeypatch):
    from cron.jobs import rearm_oneshot

    job = create_job("remind me", "in 30m", name="reminder")
    clock["now"] += timedelta(minutes=30, seconds=5)
    assert _due_ids() == [job["id"]]
    claimed, _record = _start(job["id"])
    _book_stop(job["id"], claimed)
    new_time = clock["now"] + timedelta(hours=3)
    rearm_oneshot(job["id"], new_time)

    assert _restart(monkeypatch) == 1

    after = get_job(job["id"])
    assert after.get("manual_run_at") is None and after.get("restart_rerun") is None
    assert after["next_run_at"] == new_time.isoformat()
    assert _due_ids() == []


@pytest.mark.parametrize("change", ["prompt", "repeat", None])
def test_imported_definition_counts_as_an_edit_only_when_it_changes(clock, monkeypatch, change):
    from cron.job_definition import JOB_DEFINITION_FIELDS, import_job_definitions

    job = _interrupted_hourly(clock, "shipped")
    authored = {key: value for key, value in get_job(job["id"]).items()
                if key in JOB_DEFINITION_FIELDS}
    if change == "prompt":
        authored["prompt"] = "a new prompt"
    elif change == "repeat":
        authored["repeat"] = {"times": 5}
    import_job_definitions({job["id"]: authored}, paused_reason="new")

    assert _restart(monkeypatch) == 1

    assert (get_job(job["id"]).get("restart_rerun") is None) is (change is not None)


def test_restart_window_counts_from_the_stop_bookkeeping(clock, monkeypatch):
    """A graceful stop clears the fire claim and stamps last_run_at: that is the owner's last
    sign of life, however long ago the attempt was claimed."""
    from cron.restart_rerun import RESTART_WINDOW_SECONDS

    job = create_job("long job", "every 6h", name="long")
    clock["now"] += timedelta(hours=6, minutes=1)
    assert _due_ids() == [job["id"]]
    claimed, _record = _start(job["id"])
    clock["now"] += timedelta(seconds=RESTART_WINDOW_SECONDS + 600)
    _book_stop(job["id"], claimed)
    clock["now"] += timedelta(minutes=2)

    assert _restart(monkeypatch) == 1

    assert get_job(job["id"])["next_run_at"] == clock["now"].isoformat()


def test_scheduler_down_longer_than_a_restart_is_left_to_missed_run_policy(clock, monkeypatch):
    """A stop with a run in flight and a start two days later is no restart: the old one-shot
    must not fire at once past its grace window."""
    from cron.restart_rerun import RESTART_WINDOW_SECONDS

    job = create_job("remind me", "in 30m", name="reminder")
    clock["now"] += timedelta(minutes=30, seconds=5)
    assert _due_ids() == [job["id"]]
    claimed, _record = _start(job["id"])
    _book_stop(job["id"], claimed)
    clock["now"] += timedelta(seconds=RESTART_WINDOW_SECONDS + 1)

    assert _restart(monkeypatch) == 1

    after = get_job(job["id"])
    assert (after["state"], after.get("restart_rerun")) == ("completed", None)


def test_restart_window_counts_from_the_last_claim_heartbeat(clock, monkeypatch):
    """A long run's fire claim is refreshed while it runs: the window starts at its last
    heartbeat (the owner's death), not at the claim."""
    from cron.jobs import heartbeat_fire_claim
    from cron.restart_rerun import RESTART_WINDOW_SECONDS

    job = create_job("long job", "every 6h", name="long")
    clock["now"] += timedelta(hours=6, minutes=1)
    assert _due_ids() == [job["id"]]
    claimed, _record = _start(job["id"])
    clock["now"] += timedelta(hours=2)
    assert heartbeat_fire_claim(job["id"], expected_owner=claimed["fire_claim"]["by"])
    clock["now"] += timedelta(seconds=RESTART_WINDOW_SECONDS - 60)

    assert _restart(monkeypatch) == 1

    assert get_job(job["id"])["next_run_at"] == clock["now"].isoformat()


def test_busy_fire_fence_is_logged_and_skipped(clock, monkeypatch, caplog):
    job = _interrupted_hourly(clock, "fenced")
    natural_next = get_job(job["id"])["next_run_at"]
    monkeypatch.setattr("cron.jobs._under_fire_fence", lambda _job_id, _fn: False)

    with caplog.at_level(logging.INFO, logger="cron.jobs"):
        assert _restart(monkeypatch) == 1

    assert "its fire fence was busy" in caplog.text
    assert get_job(job["id"])["next_run_at"] == natural_next


def test_reaps_outside_startup_settle_without_rerunning(clock, monkeypatch):
    """The live gateway's periodic dead-owner reap, the cronjob tool and the worker waiter also
    recover: an owner dying under them (a killed `hermes cron run`) was not cut off by a restart."""
    job = _interrupted_hourly(clock, "killed cli run")
    natural_next = get_job(job["id"])["next_run_at"]
    _kill_owner(monkeypatch)

    assert executions.recover_interrupted_executions() == 1

    after = get_job(job["id"])
    assert after["next_run_at"] == natural_next and after.get("restart_rerun") is None
    assert executions.latest_execution(job["id"])["status"] == "unknown"


def _run_ticker(job_id, tmp_path, multiplex, gate=None):
    """Run the in-process ticker's real start() until its first tick (or 2 s when the gate
    hands it nothing); return the job's next_run_at as that first tick saw it."""
    import threading

    from cron.scheduler_provider import InProcessCronScheduler

    stop = threading.Event()
    seen: list[str] = []

    def _tick(*_args, **_kwargs):
        seen.append(get_job(job_id)["next_run_at"])
        stop.set()
        return 0

    kwargs: dict = {"interval": 0}
    if multiplex:
        kwargs["profile_homes"] = [("default", tmp_path)]
        if gate is not None:
            kwargs["profile_gate"] = gate
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("cron.scheduler.tick", _tick)
        thread = threading.Thread(target=InProcessCronScheduler().start, args=(stop,),
                                  kwargs=kwargs, daemon=True)
        thread.start()
        thread.join(timeout=2 if gate is not None else 10)
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    return seen[0] if seen else None


@pytest.mark.parametrize("multiplex", [False, True])
def test_ticker_startup_recovery_reruns_the_cut_off_attempt(clock, monkeypatch, tmp_path, multiplex):
    """Entry path: the in-process ticker's startup recovery (single-profile and multiplexed)."""
    job = _interrupted_hourly(clock, "startup")
    _kill_owner(monkeypatch)

    assert _run_ticker(job["id"], tmp_path, multiplex) == clock["now"].isoformat()
    assert get_job(job["id"])["restart_rerun"]["execution_id"]
    assert executions.startup_recovery_active() is False


def test_ticker_restarted_mid_life_does_not_rerun(clock, monkeypatch, tmp_path):
    """restart_if_dead / a chronos fallback re-enter start() in a process that has already run
    its startup recovery: a dead owner then is no restart of this process."""
    first = _interrupted_hourly(clock, "first")
    _kill_owner(monkeypatch)
    assert _run_ticker(first["id"], tmp_path, multiplex=False) == clock["now"].isoformat()
    _finish(first["id"])

    clock["now"] += timedelta(minutes=5)
    second = create_job("killed later", "every 60m", name="later")
    clock["now"] += timedelta(minutes=61)
    assert second["id"] in _due_ids()
    _start(second["id"])
    natural_next = get_job(second["id"])["next_run_at"]
    _kill_owner(monkeypatch)

    _run_ticker(second["id"], tmp_path, multiplex=False)

    after = get_job(second["id"])
    assert after["next_run_at"] == natural_next and after.get("restart_rerun") is None


@pytest.mark.parametrize("gate", ["rejects", "raises"])
def test_desktop_ticker_does_not_rerun_a_profile_its_gate_hands_to_a_gateway(
        clock, monkeypatch, tmp_path, gate):
    """The Desktop backend's multiplexed ticker recovers every profile at startup, but a profile
    its gate rejects is owned by a live gateway: an owner dying there was no restart."""
    job = _interrupted_hourly(clock, "gateway owned")
    natural_next = get_job(job["id"])["next_run_at"]
    _kill_owner(monkeypatch)

    def _gate(_name, _home):
        if gate == "raises":
            raise RuntimeError("gate broke")
        return False

    _run_ticker(job["id"], tmp_path, multiplex=True, gate=_gate)

    after = get_job(job["id"])
    assert after["next_run_at"] == natural_next and after.get("restart_rerun") is None
    assert executions.latest_execution(job["id"])["status"] == "unknown"


def test_desktop_ticker_reruns_a_profile_its_gate_admits(clock, monkeypatch, tmp_path):
    job = _interrupted_hourly(clock, "desktop owned")
    _kill_owner(monkeypatch)

    seen = _run_ticker(job["id"], tmp_path, multiplex=True, gate=lambda _name, _home: True)

    assert seen == clock["now"].isoformat()
    assert get_job(job["id"])["restart_rerun"]["execution_id"]


def test_chronos_fallback_ticker_does_not_rerun(clock, monkeypatch, tmp_path):
    """Chronos start() recovers without rerunning ("never requeue"); its built-in fallback ticker,
    started mid-life when NAS rejects the identity, must not take its own start() for a restart."""
    import threading

    from plugins.cron_providers.chronos import ChronosCronScheduler

    class _Nas:
        def provision(self, **_kwargs):
            return {"schedule_id": "s"}

        def cancel(self, **_kwargs):
            return {}

        def list_armed(self):
            return []

    chronos = ChronosCronScheduler()
    chronos._client = _Nas()
    chronos.start(threading.Event(), interval=0)
    # Mid-life: an attempt (a `hermes cron run`) dies, then NAS rejects the identity.
    job = _interrupted_hourly(clock, "chronos")
    natural_next = get_job(job["id"])["next_run_at"]
    _kill_owner(monkeypatch)

    _run_ticker(job["id"], tmp_path, multiplex=False)

    after = get_job(job["id"])
    assert executions.latest_execution(job["id"])["status"] == "unknown"
    assert after["next_run_at"] == natural_next and after.get("restart_rerun") is None


# --- [daveey] a host asleep through the rerun (cron-7dfb141525) ------------------------------

def _owner_cut(job_id, clock, *, awake=None, wall=None, minutes=1):
    """Claim the job's due fire, then the owner reports its restart cut it with this clock."""
    from cron.restart_rerun import reschedule_interrupted

    claimed, record = _start(job_id)
    clock["now"] += timedelta(minutes=minutes)
    # What settle_restart_cut books before it reschedules (the restart-window clock included).
    assert mark_job_run(job_id, False, "cut", shutdown_interrupted=True, execution_id=record["id"],
                        expected_fire_owner=claimed["fire_claim"]["by"])
    executions.finish_execution(record["id"], success=False, error="cut")
    return record, reschedule_interrupted(executions.get_execution(record["id"]), restart_cut=True,
                                          awake_seconds=awake, wall_seconds=wall)


@pytest.fixture()
def awake_host(monkeypatch):
    state = {"awake": True}
    monkeypatch.setattr("cron.restart_rerun.host_fully_awake", lambda: state["awake"])
    return state


def test_rerun_cut_while_the_host_slept_is_rearmed(clock, awake_host, caplog):
    job = create_job("post", "every 120m", name="post")
    clock["now"] += timedelta(minutes=121)
    natural_next = None
    _first, rerun_armed = _owner_cut(job["id"], clock, awake=900, wall=900)
    assert rerun_armed
    natural_next = get_job(job["id"])[extra_fire_key()]
    with caplog.at_level(logging.INFO, logger="cron.jobs"):
        rerun, again = _owner_cut(job["id"], clock, awake=30, wall=3 * 3600, minutes=50)
    assert again, caplog.text
    assert f"cron: rescheduled {job['id']} (execution {rerun['id']}) {RESCHEDULED} (deferred rerun 2/3" in caplog.text
    after = get_job(job["id"])
    assert after["restart_rerun"]["reruns"] == 2 and not after["restart_rerun"].get("settled")
    assert after[extra_fire_key()] == natural_next, "the natural slot is kept"
    assert _due_ids() == [job["id"]]


def extra_fire_key():
    from cron import extra_fire

    return extra_fire.RESTORE_KEY


def test_rerun_cut_after_ten_awake_minutes_is_not_rearmed(clock, awake_host):
    job = create_job("post", "every 120m", name="post")
    clock["now"] += timedelta(minutes=121)
    _owner_cut(job["id"], clock, awake=900, wall=900)
    _rerun, again = _owner_cut(job["id"], clock, awake=600, wall=700)
    assert not again
    assert get_job(job["id"])["restart_rerun"]["settled"] is True
    assert _due_ids() == []


def test_unmeasured_rerun_keeps_rerun_once(clock, awake_host):
    job = create_job("post", "every 120m", name="post")
    clock["now"] += timedelta(minutes=121)
    _owner_cut(job["id"], clock)
    _rerun, again = _owner_cut(job["id"], clock)
    assert not again and _due_ids() == []


def test_slept_rerun_cap_is_three_per_original_cut(clock, awake_host):
    job = create_job("post", "every 600m", name="post")
    clock["now"] += timedelta(minutes=601)
    assert _owner_cut(job["id"], clock, awake=5, wall=5)[1]  # the original cut: rerun 1
    for expected in (2, 3):
        assert _owner_cut(job["id"], clock, awake=5, wall=1800)[1]
        assert get_job(job["id"])["restart_rerun"]["reruns"] == expected
    _rerun, again = _owner_cut(job["id"], clock, awake=5, wall=1800)
    assert not again, "three reruns per original cut, then the schedule"
    assert get_job(job["id"])["restart_rerun"]["settled"] is True
    assert _due_ids() == []


@pytest.mark.parametrize("awake,wall,slept", [
    (None, None, False), (599, 599, True), (600, 600, False),
    (700, 3 * 3600, True),  # ~10 awake minutes in 3 h 17 m: the incident's rerun
    (1200, 1700, False), (True, 10, False), (-1, 10, False)])
def test_slept_through_reads_the_owner_clock(awake, wall, slept):
    from cron.restart_rerun import slept_through

    assert slept_through(awake, wall) is slept


def test_pending_rerun_waits_for_full_wake_until_the_next_slot(clock, awake_host, caplog):
    job = create_job("post", "every 120m", name="post")
    clock["now"] += timedelta(minutes=121)
    awake_host["awake"] = False
    assert _owner_cut(job["id"], clock, awake=900, wall=900)[1]
    assert _due_ids() == [], "a restart rerun never fires into a DarkWake"
    awake_host["awake"] = True
    assert _due_ids() == [job["id"]], "it fires on the first fully awake tick"

    # Held all the way to the natural slot: that slot settles it (no double run).
    awake_host["awake"] = False
    slot = get_job(job["id"])[extra_fire_key()]
    clock["now"] = datetime.fromisoformat(slot) + timedelta(seconds=1)
    with caplog.at_level(logging.WARNING, logger="cron.jobs"):
        assert _due_ids() == []
    assert f"pending restart rerun of {job['id']} settled by its next slot" in caplog.text
    after = get_job(job["id"])
    assert after["next_run_at"] == slot and after.get("manual_run_at") is None
    assert extra_fire_key() not in after and after["restart_rerun"]["settled"] is True
    assert _due_ids() == [job["id"]], "the slot fires as itself"
    claimed = claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict) and not claimed["fire_claim"].get("extra_slot")


def test_oneshot_rerun_held_past_grace_fires_on_full_wake(clock, awake_host):
    # A one-shot rerun held through a DarkWake longer than the one-shot grace window is the cut
    # work redone, not a missed one-shot: the grace gate must not retire it.
    job = create_job("remind me", "in 30m", name="reminder")
    clock["now"] += timedelta(minutes=31)
    awake_host["awake"] = False
    assert _owner_cut(job["id"], clock, awake=900, wall=900)[1]
    assert _due_ids() == []
    clock["now"] += timedelta(minutes=10)
    assert _due_ids() == [], "still held in the DarkWake"
    awake_host["awake"] = True
    assert _due_ids() == [job["id"]], "it fires on the first fully awake tick"
    assert get_job(job["id"]) is not None


@pytest.mark.parametrize("schedule", ["in 30m", "every 120m"])
def test_deferred_pending_rerun_stays_held_and_survives_the_oneshot_grace(clock, awake_host, schedule):
    """A pending rerun the draining owner refused (defer_unadmitted) is re-armed for now but keeps
    the rerun's own time: it is still the pending rerun, so it is held in a DarkWake and the
    one-shot grace gate never retires it as missed."""
    from cron.jobs import ONESHOT_GRACE_SECONDS
    from cron.restart_cut import defer_unadmitted
    from cron.restart_rerun import pending_rerun

    job = create_job("remind me", schedule, name="reminder")
    clock["now"] += timedelta(minutes=31 if schedule.startswith("in") else 121)
    assert _owner_cut(job["id"], clock, awake=900, wall=900)[1]
    clock["now"] += timedelta(minutes=1)
    claimed, record = _start(job["id"])
    assert defer_unadmitted(claimed, record["id"], "runtime_draining", claimed["fire_claim"]["by"])
    deferred = get_job(job["id"])
    assert deferred["next_run_at"] == clock["now"].isoformat(), "re-armed for the next gateway"
    assert pending_rerun(deferred), f"the deferred rerun is no longer pending: {deferred['restart_rerun']}"

    awake_host["awake"] = False
    assert _due_ids() == [], "a deferred restart rerun never fires into a DarkWake"
    clock["now"] += timedelta(seconds=ONESHOT_GRACE_SECONDS + 60)
    assert _due_ids() == [], "still held in the DarkWake"
    assert get_job(job["id"]) is not None, "held past the one-shot grace, never retired as missed"
    awake_host["awake"] = True
    assert _due_ids() == [job["id"]], "it fires on the first fully awake tick"


def test_full_wake_probe_reads_pmset_assertions(monkeypatch):
    from cron import restart_rerun

    # The probe's parsing, host-independent: _on_macos is the seam, never the interpreter's platform.
    monkeypatch.setattr(restart_rerun, "_on_macos", lambda: True)
    outputs = {}
    monkeypatch.setattr(restart_rerun, "_pmset", lambda what: outputs[what])
    monkeypatch.setattr(restart_rerun, "_lid_closed", lambda: outputs["lid"])
    outputs.update(assertions="Assertion status system-wide:\n   UserIsActive                   1\n",
                   batt="Now drawing from 'Battery Power'", lid=True)
    assert restart_rerun.host_fully_awake()
    outputs["assertions"] = "Assertion status system-wide:\n   UserIsActive                   0\n"
    assert not restart_rerun.host_fully_awake(), "a DarkWake on battery"
    outputs.update(batt="Now drawing from 'AC Power'", lid=False)
    assert restart_rerun.host_fully_awake(), "lid open on AC"
    outputs["lid"] = True
    assert not restart_rerun.host_fully_awake(), "clamshell on AC with no user is a DarkWake"

    def broken(_what):
        raise OSError("no pmset")

    monkeypatch.setattr(restart_rerun, "_pmset", broken)
    assert restart_rerun.host_fully_awake(), "a broken probe never holds a rerun"
    monkeypatch.setattr(restart_rerun, "_on_macos", lambda: False)
    monkeypatch.setattr(restart_rerun, "_pmset", lambda _w: pytest.fail("probed off macOS"))
    assert restart_rerun.host_fully_awake()
