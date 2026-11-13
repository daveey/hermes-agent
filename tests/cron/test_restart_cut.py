"""[daveey] A cron run its owner gateway's restart cut short is rerun once, never booked a failure.

The three live shapes from an instant hub core update (t_01a1d009633f359e):

1. A run in flight when the owner drains (``hermes cron run`` or a ticker fire whose firer survives):
   the owner reports the cut (``_restart_cut``) and the firer books an interruption + one rerun.
2. A fire the owner refused because it was draining: deferred to the next gateway, not failed.
3. A restart-safe worker that lost the owner mid-run: the new gateway's journal recovery reads the
   lost (``interrupted``) admission and reruns the job once instead of booking ``failed``.
"""
import asyncio
from contextlib import nullcontext
import json
import threading
from types import SimpleNamespace

import pytest

from cron import executions, jobs, scheduler, scheduler_authority

FAR_FUTURE = "0 0 1 1 *"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("cron.executions._end_interrupted_sessions", lambda _records: None)
    scheduler._running_job_ids.clear()
    scheduler._running_fire_owners.clear()
    scheduler._interrupted_job_ids.clear()
    yield tmp_path
    scheduler._running_job_ids.clear()
    scheduler._running_fire_owners.clear()
    scheduler._interrupted_job_ids.clear()


def _due(job_id):
    return job_id in [job["id"] for job in jobs.get_due_jobs()]


def _assert_rerun_once(job_id, execution_id, *, natural_next):
    job = jobs.get_job(job_id)
    assert job["restart_rerun"]["execution_id"] == execution_id
    assert int(job.get("failure_streak") or 0) == 0
    assert job.get("last_error_kind") is None
    assert _due(job_id), "the cut run must be due again at once"
    # The rerun claims, runs and settles; the far-future slot is restored.
    claimed = jobs.claim_job_for_fire(job_id, return_job=True)
    assert isinstance(claimed, dict)
    assert jobs.mark_job_run(job_id, True, expected_fire_owner=claimed["fire_claim"]["by"])
    done = jobs.get_job(job_id)
    assert done["last_status"] == "ok" and done.get("restart_rerun") is None
    assert done["next_run_at"] == natural_next
    assert not _due(job_id)


# --- shape 1: the firer books the owner-reported cut ------------------------------------------

def test_direct_run_cut_by_owner_restart_reruns_once_without_a_failure(home, monkeypatch):
    job = jobs.create_job(prompt="probe", schedule=FAR_FUTURE, name="probe", deliver="local")
    natural_next = job["next_run_at"]
    claimed = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
    assert isinstance(claimed, dict)
    delivered = []

    def run_job(job, **_kwargs):
        # The owner's drain stopped the run and said so on the canonical result
        # (gateway/session_cron.py); run_canonical_job merges its job flags into the firer's job.
        job["_restart_cut"] = True
        return (False, "partial", "", "RuntimeError: Cron job was stopped: cron fire ownership was lost")

    monkeypatch.setattr(scheduler, "run_job", run_job)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **k: delivered.append(a))
    assert scheduler.run_one_job(claimed) is True
    execution_id = claimed["execution_id"]
    row = executions.get_execution(execution_id)
    assert row["status"] == "failed" and "gateway restart" in row["error"]
    assert row["delivery_outcome"] == "suppressed"
    assert delivered == [] and scheduler.drain_delivery_queue({}, None) == 0, "no failure notice"
    # Typed, so consumers (the coach) need not match the prose detail.
    assert jobs.get_job(job["id"])["last_failure"]["kind"] == "lifecycle_interrupt"
    _assert_rerun_once(job["id"], execution_id, natural_next=natural_next)


def test_rerun_cut_again_is_not_rerun_a_second_time(home, monkeypatch):
    job = jobs.create_job(prompt="probe", schedule=FAR_FUTURE, name="probe", deliver="local")

    def run_job(job, **_kwargs):
        job["_restart_cut"] = True
        return (False, "", "", "interrupted")

    monkeypatch.setattr(scheduler, "run_job", run_job)
    first = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
    assert scheduler.run_one_job(first) is True
    assert _due(job["id"])
    rerun = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(rerun, dict)
    assert scheduler.run_one_job(rerun) is True
    saved = jobs.get_job(job["id"])
    assert saved["restart_rerun"]["execution_id"] == rerun["execution_id"]
    assert saved["restart_rerun"]["settled"] is True
    from cron.restart_rerun import reschedule_interrupted

    assert not reschedule_interrupted(executions.get_execution(rerun["execution_id"]), restart_cut=True)
    assert jobs.get_job(job["id"]) == saved
    assert not _due(job["id"]), "a rerun cut again waits for its schedule"
    assert int(saved.get("failure_streak") or 0) == 0


def _cut_with_clock(awake, wall):
    def run_job(job, **_kwargs):
        # The owner's drain stopped the run and measured it (gateway/session_cron.py).
        job["_restart_cut"] = True
        job["_run_clock"] = {"awake": awake, "wall": wall}
        return (False, "", "", "interrupted")
    return run_job


def test_owner_cut_of_a_rerun_the_host_slept_through_is_rerun_again(home, monkeypatch):
    """cron-7dfb141525: a MacBook asleep on battery; the restart cut the run and the immediate
    rerun was cut by the same sleep after ~30 awake seconds in an hour. That rerun never ran, so it
    is re-armed (rerun 2 of 3), not dropped."""
    job = jobs.create_job(prompt="probe", schedule=FAR_FUTURE, name="probe", deliver="local")
    monkeypatch.setattr("cron.restart_rerun.host_fully_awake", lambda: True, raising=False)
    monkeypatch.setattr(scheduler, "run_job", _cut_with_clock(900, 900))
    first = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
    assert scheduler.run_one_job(first) is True
    assert _due(job["id"])
    monkeypatch.setattr(scheduler, "run_job", _cut_with_clock(30, 3600))
    rerun = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(rerun, dict)
    assert scheduler.run_one_job(rerun) is True
    marker = jobs.get_job(job["id"])["restart_rerun"]
    assert not marker.get("settled"), f"the slept-through rerun was dropped: {marker}"
    assert marker["execution_id"] == rerun["execution_id"] and marker["reruns"] == 2
    assert _due(job["id"])
    assert int(jobs.get_job(job["id"]).get("failure_streak") or 0) == 0


def test_owner_cut_of_a_rerun_that_was_awake_ten_minutes_stops(home, monkeypatch):
    job = jobs.create_job(prompt="probe", schedule=FAR_FUTURE, name="probe", deliver="local")
    monkeypatch.setattr(scheduler, "run_job", _cut_with_clock(900, 900))
    first = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
    assert scheduler.run_one_job(first) is True
    rerun = jobs.claim_job_for_fire(job["id"], return_job=True)
    monkeypatch.setattr(scheduler, "run_job", _cut_with_clock(601, 700))
    assert scheduler.run_one_job(rerun) is True
    assert jobs.get_job(job["id"])["restart_rerun"]["settled"] is True
    assert not _due(job["id"]), "a rerun that really ran keeps rerun-once"


def test_an_ordinary_failure_still_counts(home, monkeypatch):
    job = jobs.create_job(prompt="probe", schedule=FAR_FUTURE, name="probe", deliver="local")
    monkeypatch.setattr(scheduler, "run_job", lambda job, **_k: (False, "", "", "agent broke"))
    claimed = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
    assert scheduler.run_one_job(claimed) is True
    saved = jobs.get_job(job["id"])
    assert saved["failure_streak"] == 1 and saved.get("restart_rerun") is None
    assert "kind" not in saved["last_failure"], "an ordinary failure is not a lifecycle interrupt"
    assert not _due(job["id"])


# --- the owner reports the cut ----------------------------------------------------------------

def _owner(tmp_path, *, draining=False):
    from gateway.session_authority import LiveSession, SessionAuthority
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch

    db = SessionDB(tmp_path / "state.db")
    db.create_session("s", source="cron")
    runner = SimpleNamespace(_draining=draining)
    authority = SessionAuthority(runner, profile_id="p", instance_id="test", db=db,
                                 epoch=begin_runtime_epoch(db, instance_id="test"))
    authority.sessions["s"] = LiveSession(SimpleNamespace(platform=None, user_id="cron-owner"), "route")
    return authority, db


@pytest.mark.parametrize("case", ["interrupting", "claim_cleared", "operator_cancel", "not_draining",
                                  "finished_anyway", "returned_after_stop_finished"])
def test_owner_marks_only_its_own_restart_cut(home, monkeypatch, case):
    from cron.cancellation import cancel_is_shutdown, request_cancel
    from gateway import run, session_cron
    from gateway.session_contract import SessionRef
    from hermes_state_runtime import admit_session_input, claim_session_input

    authority, db = _owner(home, draining=case != "not_draining")
    monkeypatch.setattr(run, "_profile_runtime_scope", lambda home: nullcontext())
    execution = executions.create_execution("job", source="direct")
    request_id = "cron:job:" + execution["id"]
    captured = {}

    def run_job(job, *, extra_prompt, execution_id, cancel_event):
        if case == "interrupting":
            authority.runner._interrupting_remaining_work = True
        elif case == "returned_after_stop_finished":
            # Live hub shape: the drain timed out and interrupted the run, then stop() finished and
            # reset ``_draining`` before this thread unwound (it outlives the executor quiesce).
            authority.runner._interrupting_remaining_work = True
            authority._cron_cancellations[row["admission_id"]].set()
            authority.runner._draining = False
            captured["shutdown"] = cancel_is_shutdown(cancel_event)
        elif case == "operator_cancel":
            request_cancel(execution_id, reason="stop it")
            authority._cron_cancellations[row["admission_id"]].set()
        else:
            authority._cron_cancellations[row["admission_id"]].set()  # the firer forwarded a cancel
        if case == "finished_anyway":
            return (True, "doc", "answer", None)
        return (False, "", "", "interrupted")

    monkeypatch.setattr(scheduler, "run_job", run_job)

    async def probe():
        with db:
            admit_session_input(db, epoch=authority.epoch, principal_id="cron-owner", session_id="s",
                                request_id=request_id, payload={"text": ""})
            nonlocal row
            row = claim_session_input(db, epoch=authority.epoch, session_id="s")
            policy = SimpleNamespace(request_json=json.dumps(
                {"extra_prompt": None, "request_id": request_id, "cron_job": {"id": "job"}}))
            try:
                await session_cron.execute(authority, SessionRef("p", "s"), row, policy)
            except RuntimeError:
                pass
        return authority.pending_results[row["admission_id"]]["result"]

    row = None
    try:
        result = asyncio.run(probe())
    finally:
        db.close()
    cut = result["cron_job_flags"].get("_restart_cut", False)
    assert cut is (case in {"interrupting", "claim_cleared", "returned_after_stop_finished"})
    if case == "returned_after_stop_finished":
        assert captured == {"shutdown": True}  # logged as this gateway's shutdown, not lost ownership
    if cut:
        assert result["cron_result"][0] is False


def test_status_of_an_admission_lost_across_the_restart_reports_the_cut(home):
    from gateway import session_cron
    from gateway.session_contract import SessionActor
    from hermes_state_keys import CRON_PRINCIPAL_ID
    from hermes_state_runtime import (admit_session_input, claim_session_input, recover_session_inputs,
                                      begin_runtime_epoch)
    from gateway.session_unattended_unknown import settle_recovered_unattended

    authority, db = _owner(home)
    actor = SessionActor(CRON_PRINCIPAL_ID, "p",
                         frozenset({"session:create", "session:submit", "session:control"}), "ticker")
    authority.sessions["s"].source.user_id = CRON_PRINCIPAL_ID
    try:
        with db:
            receipt = admit_session_input(db, epoch=authority.epoch, principal_id=CRON_PRINCIPAL_ID,
                                          session_id="s", request_id="cron:job:fire", payload={"text": ""})
            claim_session_input(db, epoch=authority.epoch, session_id="s")
        # The owner process dies mid-run; the next one fences the lost turn and settles it.
        epoch = begin_runtime_epoch(db, instance_id="next")
        recover_session_inputs(db, epoch=epoch)
        assert settle_recovered_unattended(db, epoch) == 1
        authority.epoch = epoch
        state = asyncio.run(session_cron.operation(
            authority, "status", {"session_id": "s", "admission_id": receipt["admission_id"]}, actor))
    finally:
        db.close()
    assert state["status"] == "terminal"
    assert state["result"][0] is False
    assert state["job_flags"].get("_restart_cut") is True


# --- shapes 2 and 3: journal recovery in the new gateway --------------------------------------

@pytest.fixture
def owner_ops(monkeypatch):
    from gateway import session_cron

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    handlers = {}
    monkeypatch.setattr(session_cron, "owner_for_home", lambda home: (object(), loop))

    async def operation(authority, op, params, actor=None):
        return handlers[op](params)

    monkeypatch.setattr(session_cron, "operation", operation)
    try:
        yield handlers
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def _dead_firer(monkeypatch):
    monkeypatch.setattr(executions, "_PROCESS_ID", "replacement")
    monkeypatch.setattr(executions, "_owner_is_live", lambda _pid, _started: False)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)


def test_restart_safe_worker_cut_is_rerun_by_journal_recovery(home, monkeypatch, owner_ops):
    """Hub sample 3: a builtin fire on a far-future schedule, its restart-safe worker lost the owner
    (paused, ``unknown``); the new owner reports the lost turn as a restart cut."""
    job = jobs.create_job(prompt="probe", schedule="45 13 12 11 *", name="probe", deliver="local")
    claimed = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
    assert jobs.claim_dispatch(job["id"])
    fire = executions.create_execution(job["id"], source="builtin",
                                       scheduled_instant=claimed["next_run_at"])["id"]
    executions.mark_execution_running(fire)
    journal = scheduler_authority.journal_path(job["id"], fire)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"params": {"job_id": job["id"], "request_id": fire, "extra_prompt": None},
                                   "receipt": {"session_id": "s", "admission_id": "a"}}))
    scheduler_authority.pause_for_unknown(job["id"], fire, "Cron execution unverified: runtime_draining")
    executions.give_up_execution(fire, reason="Restart-safe cron worker exited with status 1")
    _dead_firer(monkeypatch)
    natural_next = jobs.get_job(job["id"])["next_run_at"]
    owner_ops["recover"] = lambda params: {
        "status": "terminal", "result": [False, "", "", "interrupted"], "job": jobs.get_job(job["id"]),
        "job_flags": {"_restart_cut": True}}
    monkeypatch.setattr(scheduler, "_save_compose_deliver",
                        lambda *a, **k: pytest.fail("a cut run sends no failure notice"))

    scheduler_authority.reconcile_pending()

    assert not journal.exists()
    row = executions.get_execution(fire)
    assert row["status"] == "failed" and "gateway restart" in row["error"]
    saved = jobs.get_job(job["id"])
    assert saved["enabled"] is True, "its uncertainty pause is lifted"
    assert saved["last_error"] != "interrupted"
    scheduler_authority.reconcile_pending()  # a second pass changes nothing
    assert jobs.get_job(job["id"])["restart_rerun"]["execution_id"] == fire
    _assert_rerun_once(job["id"], fire, natural_next=natural_next)


@pytest.mark.parametrize("deleted", [False, True])
def test_oneshot_due_scan_before_journal_recovery(home, monkeypatch, owner_ops, deleted):
    """A replacement ticker may scan before the canonical owner can reconcile its journal."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    job = jobs.create_job(prompt="probe", schedule="in 1m", repeat=1, deliver="local")
    now += timedelta(minutes=1, seconds=1)
    assert _due(job["id"])
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert jobs.claim_dispatch(job["id"])
    fire = executions.create_execution(job["id"], source="builtin")["id"]
    executions.mark_execution_running(fire)
    journal = scheduler_authority.journal_path(job["id"], fire)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"params": {"job_id": job["id"], "request_id": fire,
                                              "extra_prompt": None}, "receipt": None}))
    _dead_firer(monkeypatch)
    owner_ops["recover"] = lambda params: {
        "status": "terminal", "result": [False, "", "", "interrupted"], "job": claimed,
        "job_flags": {"_restart_cut": True}}
    if deleted:
        assert jobs.remove_job(job["id"])
    # Headless tick cannot dial an absent owner; housekeeping leaves the journal pending,
    # but still performs its real due scan before the replacement gateway can reconcile.
    with monkeypatch.context() as headless:
        headless.setattr("gateway.session_cron.owner_for_home", lambda home: None)
        headless.setattr(scheduler, "_maybe_run_worktree_maintenance", lambda: None)
        headless.setattr(scheduler, "_sweep_mcp_orphans", lambda: None)
        assert scheduler.tick(verbose=False, headless=True) == 0
    assert not _due(job["id"])
    if not deleted:
        assert jobs.get_job(job["id"]) is not None
        assert not jobs.claim_dispatch(job["id"])
        assert jobs.get_job(job["id"]) is not None
    scheduler_authority.reconcile_pending()
    scheduler_authority.reconcile_pending()
    if deleted:
        assert jobs.get_job(job["id"]) is None
        assert not _due(job["id"])
        return
    saved = jobs.get_job(job["id"])
    assert saved["repeat"] == {"times": 1, "completed": 0}
    assert int(saved.get("failure_streak") or 0) == 0
    assert not journal.exists()
    assert _due(job["id"])
    rerun = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert jobs.claim_dispatch(job["id"])
    assert jobs.mark_job_run(job["id"], True, expected_fire_owner=rerun["fire_claim"]["by"])
    scheduler_authority.reconcile_pending()
    saved = jobs.get_job(job["id"])
    assert saved["repeat"] == {"times": 1, "completed": 1}
    assert saved["state"] == "completed" and saved["last_status"] == "ok"
    assert not _due(job["id"])


def _corrupt_journal(shape, job_id):
    """A damaged admission journal: unreadable, or naming this job under the wrong key."""
    payload = {"json": "{not json", "no-params": json.dumps({"receipt": None}),
               "not-a-dict": json.dumps([1]), "bad-params": json.dumps({"params": None}),
               "misnamed": json.dumps({"params": {"job_id": job_id, "request_id": "r1"}})}[shape]
    path = scheduler_authority.journal_path(job_id, "").parent / f"{shape}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload)
    return path


@pytest.mark.parametrize("shape", ["json", "no-params", "not-a-dict", "bad-params", "misnamed"])
def test_oneshot_corrupt_journal_holds_and_names_the_journal(home, monkeypatch, owner_ops, caplog,
                                                             shape):
    """[daveey] A damaged journal fails closed (the exhausted one-shot is kept, the journal is never
    deleted) and the due scan names the journal, not the healthy job, as the cause."""
    from datetime import UTC, datetime, timedelta
    import logging

    now = datetime.now(UTC)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    job = jobs.create_job(prompt="probe", schedule="in 1m", repeat=1, name="probe", deliver="local")
    now += timedelta(minutes=1, seconds=1)
    jobs.claim_job_for_fire(job["id"], return_job=True)
    assert jobs.claim_dispatch(job["id"])
    fire = executions.create_execution(job["id"], source="builtin")["id"]
    executions.mark_execution_running(fire)
    corrupt = _corrupt_journal(shape, job["id"])
    _dead_firer(monkeypatch)
    caplog.set_level(logging.INFO)
    with monkeypatch.context() as headless:
        headless.setattr("gateway.session_cron.owner_for_home", lambda home: None)
        headless.setattr(scheduler, "_maybe_run_worktree_maintenance", lambda: None)
        headless.setattr(scheduler, "_sweep_mcp_orphans", lambda: None)
        assert scheduler.tick(verbose=False, headless=True) == 0
    assert jobs.get_job(job["id"]) is not None, "an uncheckable journal must not retire the job"
    assert not _due(job["id"])
    messages = [r.getMessage() for r in caplog.records]
    assert not [m for m in messages if "Skipping malformed cron job" in m], messages
    assert [m for m in messages if str(corrupt) in m and "holding the job unretired" in m], messages
    assert not jobs.claim_dispatch(job["id"])
    assert jobs.get_job(job["id"]) is not None
    scheduler_authority.reconcile_pending()
    assert corrupt.exists(), "an uncertain journal is never deleted by recovery"
    assert jobs.get_job(job["id"]) is not None
    # Healthy one-shots never reach the journal check; their dispatch is unaffected.
    other = jobs.create_job(prompt="other", schedule="in 1m", repeat=1, deliver="local")
    assert jobs.claim_dispatch(other["id"])
    # Once the operator removes the damaged journal, the old wedged-claim cleanup resumes.
    corrupt.unlink()
    assert not jobs.claim_dispatch(job["id"])
    assert jobs.get_job(job["id"]) is None


def test_claim_dispatch_race_with_corrupt_journal_holds_without_bookkeeping(home, monkeypatch, caplog):
    """[daveey] The scan saw budget left, another process spent it before this claim, and a damaged
    journal makes the check raise: hold the fire, never book a failed run or send a crash notice."""
    from datetime import UTC, datetime, timedelta
    import logging

    now = datetime.now(UTC)
    monkeypatch.setattr(jobs, "_hermes_now", lambda: now)
    job = jobs.create_job(prompt="probe", schedule="in 1m", repeat=1, name="probe", deliver="local")
    now += timedelta(minutes=1, seconds=1)
    claimed = jobs.claim_job_for_fire(job["id"], return_job=True)
    assert isinstance(claimed, dict) and claimed["repeat"]["completed"] == 0
    assert jobs.claim_dispatch(job["id"])  # the concurrent firer exhausts the one-shot
    corrupt = _corrupt_journal("json", job["id"])
    claimed["execution_id"] = executions.create_execution(job["id"], source="builtin")["id"]
    booked, notices = [], []
    monkeypatch.setattr(scheduler, "_launch_external_cron_worker", lambda _job: False)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *a, **k: booked.append(a) or True)
    monkeypatch.setattr(scheduler, "_deliver_crash_failure",
                        lambda *a, **k: notices.append(a) or (None, "suppressed"))
    monkeypatch.setattr(scheduler, "run_job", lambda *a, **k: pytest.fail("must not run"))
    caplog.set_level(logging.WARNING)

    assert scheduler.run_one_job(claimed) is True

    assert booked == [] and notices == []
    saved = jobs.get_job(job["id"])
    assert saved["repeat"] == {"times": 1, "completed": 1}
    assert saved.get("last_status") is None and int(saved.get("failure_streak") or 0) == 0
    row = executions.get_execution(claimed["execution_id"])
    assert "Dispatch claim rejected" in row["error"]
    assert corrupt.exists()
    assert [r for r in caplog.records if str(corrupt) in r.getMessage()]


def test_manual_fire_refused_while_draining_is_deferred_not_failed(home, monkeypatch, owner_ops):
    """Hub sample 2: ``hermes cron run`` while the owner drains: refused, never admitted."""
    from hermes_state_runtime import RuntimeStoreError

    job = jobs.create_job(prompt="probe", schedule=FAR_FUTURE, name="probe", deliver="local")
    natural_next = job["next_run_at"]

    def refuse(_params):
        raise RuntimeStoreError("runtime_draining")

    owner_ops["submit"] = refuse
    owner_ops["recover"] = lambda params: {"status": "missing"}
    claimed = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *a, **k: pytest.fail("no failure notice"))
    scheduler.run_one_job(claimed)

    row = executions.get_execution(claimed["execution_id"])
    assert row["status"] == "failed" and "deferred" in row["error"]
    saved = jobs.get_job(job["id"])
    assert saved.get("last_status") is None and int(saved.get("failure_streak") or 0) == 0
    assert saved["fire_claim"] is None
    assert not scheduler_authority.journal_path(job["id"], claimed["execution_id"]).exists()
    _assert_rerun_once(job["id"], claimed["execution_id"], natural_next=natural_next)


def test_draining_refusal_whose_absence_check_was_lost_is_deferred_by_recovery(home, monkeypatch, owner_ops):
    """Hub sample 2 as logged: the absence check died with the owner too, so the job paused on an
    unverified admission; the next owner proves the fire missing and recovery defers it."""
    from hermes_state_runtime import RuntimeStoreError

    job = jobs.create_job(prompt="probe", schedule=FAR_FUTURE, name="probe", deliver="local")
    natural_next = job["next_run_at"]

    def refuse(_params):
        raise RuntimeStoreError("runtime_draining")

    def gone(_params):
        raise ConnectionError("owner went away")

    owner_ops["submit"] = refuse
    owner_ops["recover"] = gone
    claimed = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
    scheduler.run_one_job(claimed)
    fire = claimed["execution_id"]
    assert not jobs.get_job(job["id"])["enabled"], "paused with unverified admission"
    assert json.loads(scheduler_authority.journal_path(job["id"], fire).read_text())["submit_refused"] == \
        "runtime_draining"

    _dead_firer(monkeypatch)
    owner_ops["recover"] = lambda params: {"status": "missing"}
    scheduler_authority.reconcile_pending()

    saved = jobs.get_job(job["id"])
    assert saved["enabled"] is True
    assert saved.get("last_status") is None and int(saved.get("failure_streak") or 0) == 0
    assert not scheduler_authority.journal_path(job["id"], fire).exists()
    _assert_rerun_once(job["id"], fire, natural_next=natural_next)


def _defer_while_draining(job_id, monkeypatch, owner_ops):
    from hermes_state_runtime import RuntimeStoreError

    def refuse(_params):
        raise RuntimeStoreError("runtime_draining")

    owner_ops["submit"] = refuse
    owner_ops["recover"] = lambda params: {"status": "missing"}
    claimed = jobs.claim_job_for_fire(job_id, return_job=True) or \
        jobs.claim_job_for_fire(job_id, force=True, return_job=True)
    assert isinstance(claimed, dict)
    scheduler.run_one_job(claimed)
    assert "deferred" in executions.get_execution(claimed["execution_id"])["error"]
    assert _due(job_id)


def _cut_next_attempt(job_id, monkeypatch):
    def run_job(job, **_kwargs):
        job["_restart_cut"] = True
        return (False, "", "", "interrupted")

    monkeypatch.setattr(scheduler, "run_job", run_job)
    attempt = jobs.claim_job_for_fire(job_id, return_job=True)
    assert isinstance(attempt, dict)
    assert scheduler.run_one_job(attempt) is True
    return attempt["execution_id"]


def test_first_run_after_a_deferral_cut_by_a_restart_is_still_rerun(home, monkeypatch, owner_ops):
    """Hub live proof (b5b0e485e75e): a run-now deferred while draining, then its real attempt on the
    new gateway cut by the next update, was logged "itself a restart rerun" and never rerun. A
    deferral re-arms a fire that never ran, so it spends no rerun."""
    job = jobs.create_job(prompt="probe", schedule=FAR_FUTURE, name="probe", deliver="local")
    natural_next = job["next_run_at"]
    _defer_while_draining(job["id"], monkeypatch, owner_ops)

    cut = _cut_next_attempt(job["id"], monkeypatch)

    saved = jobs.get_job(job["id"])
    assert int(saved.get("failure_streak") or 0) == 0
    assert executions.get_execution(cut)["delivery_outcome"] == "suppressed"
    _assert_rerun_once(job["id"], cut, natural_next=natural_next)


def test_rerun_after_a_deferral_cut_again_is_not_rerun(home, monkeypatch, owner_ops):
    """The deferral spends no rerun, but the one rerun still holds: deferral -> cut -> rerun -> cut
    waits for the schedule."""
    job = jobs.create_job(prompt="probe", schedule=FAR_FUTURE, name="probe", deliver="local")
    _defer_while_draining(job["id"], monkeypatch, owner_ops)
    _cut_next_attempt(job["id"], monkeypatch)
    assert _due(job["id"])
    rerun_cut = _cut_next_attempt(job["id"], monkeypatch)
    saved = jobs.get_job(job["id"])
    assert saved["restart_rerun"]["execution_id"] == rerun_cut
    assert saved["restart_rerun"]["settled"] is True
    assert not _due(job["id"]), "a rerun cut again waits for its schedule"


def test_later_natural_fire_deferred_after_a_skipped_rerun_gets_its_own_rerun(
        home, monkeypatch, owner_ops):
    from datetime import datetime, timedelta

    clock = [datetime.fromisoformat("2026-10-28T09:00:00-07:00")]
    monkeypatch.setattr(jobs, "_hermes_now", lambda: clock[0])
    monkeypatch.setattr(executions, "_hermes_now", lambda: clock[0])
    job = jobs.create_job(prompt="probe", schedule="every 60m", name="probe", deliver="local")
    real_run_job = scheduler.run_job
    clock[0] += timedelta(minutes=61)
    _cut_next_attempt(job["id"], monkeypatch)
    clock[0] += timedelta(minutes=1)
    _cut_next_attempt(job["id"], monkeypatch)
    assert not _due(job["id"])

    clock[0] = datetime.fromisoformat(jobs.get_job(job["id"])["next_run_at"])
    assert _due(job["id"])
    monkeypatch.setattr(scheduler, "run_job", real_run_job)
    _defer_while_draining(job["id"], monkeypatch, owner_ops)
    clock[0] += timedelta(seconds=1)
    cut = _cut_next_attempt(job["id"], monkeypatch)
    assert _due(job["id"]), "the later natural fire has not spent its restart rerun"
    saved = jobs.get_job(job["id"])
    assert saved["restart_rerun"]["execution_id"] == cut
    assert not saved["restart_rerun"].get("settled")
    clock[0] += timedelta(seconds=1)
    _cut_next_attempt(job["id"], monkeypatch)
    assert not _due(job["id"]), "the later fire also gets only one rerun"


def test_deferring_a_pending_rerun_keeps_it_a_rerun(home, monkeypatch, owner_ops):
    """A rerun refused while draining is deferred, but stays the rerun: cut again, it is not rerun."""
    job = jobs.create_job(prompt="probe", schedule=FAR_FUTURE, name="probe", deliver="local")

    def run_job(job, **_kwargs):
        job["_restart_cut"] = True
        return (False, "", "", "interrupted")

    real_run_job = scheduler.run_job
    monkeypatch.setattr(scheduler, "run_job", run_job)
    first = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
    assert scheduler.run_one_job(first) is True
    rerun_at = jobs.get_job(job["id"])["restart_rerun"]["at"]
    monkeypatch.setattr(scheduler, "run_job", real_run_job)  # the rerun goes to the draining owner
    _defer_while_draining(job["id"], monkeypatch, owner_ops)
    marker = jobs.get_job(job["id"])["restart_rerun"]
    assert marker["at"] == rerun_at and not marker.get("deferred")

    _cut_next_attempt(job["id"], monkeypatch)
    assert not _due(job["id"]), "the deferred rerun, cut again, waits for its schedule"
