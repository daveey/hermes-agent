"""Tests for #60432: cron jobs must not be silently invisible to gateway
shutdown, and a job whose tool subprocess got killed by shutdown must
never be reported as a successful run.

Covers the cron/scheduler.py primitives directly:
  - get_running_job_ids() -- thread-safe snapshot the gateway drain reads
  - mark_running_jobs_interrupted() -- called by the gateway right after
    it force-kills tool subprocesses
  - the interrupted-flag race guard in run_one_job(), which must win over
    the job's own thread finishing normally with a plausible-looking
    result AFTER its tool was already killed out from under it
"""

import threading
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    """Every test starts from a clean slate and leaves one behind, since
    these sets are module-level globals shared across the test process."""
    import cron.scheduler as sched

    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()
    yield
    sched._running_job_ids.clear()
    sched._running_fire_owners.clear()
    sched._interrupted_job_ids.clear()


class TestGetRunningJobIds:

    def test_reflects_in_flight_jobs(self):
        import cron.scheduler as sched

        sched._running_job_ids.add(sched._inflight_key("job-1"))
        sched._running_job_ids.add(sched._inflight_key("job-2"))

        result = sched.get_running_job_ids()

        assert result == frozenset({"job-1", "job-2"})

    def test_snapshot_is_immutable_and_independent(self):
        """Mutating _running_job_ids after the call must not change the
        already-returned snapshot -- callers (the gateway drain loop) rely
        on this to safely count in a tight polling loop."""
        import cron.scheduler as sched

        sched._running_job_ids.add(sched._inflight_key("job-1"))
        snapshot = sched.get_running_job_ids()
        sched._running_job_ids.add(sched._inflight_key("job-2"))

        assert snapshot == frozenset({"job-1"})


class TestMarkRunningJobsInterrupted:
    @pytest.mark.parametrize("streak,kind", [(0, None), (2, "gate"), (3, "agent")])
    def test_shutdown_preserves_failure_accounting(self, tmp_path, streak, kind):
        from cron import jobs
        import cron.scheduler as sched

        home = tmp_path / "profile"
        home.mkdir()
        with jobs.use_cron_store(home):
            job = jobs.create_job(prompt="x", schedule="every 5m", name="owned")
            job = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
            assert isinstance(job, dict)
            job["failure_streak"] = streak
            if kind is not None:
                job["last_error_kind"] = kind
            jobs.save_jobs([job])
            key = sched._inflight_key(job["id"])
            sched._running_job_ids.add(key)
            sched._running_fire_owners[key] = {object(): (job["fire_claim"]["by"], home)}
            reason = "Gateway shutdown (post-interrupt) killed the job's tool subprocess."
            assert sched.mark_running_jobs_interrupted(reason) == [job["id"]]
            saved = jobs.load_jobs()[0]
            assert saved["last_status"] == "error"
            assert saved["last_failure"]["detail"] == reason
            assert saved["last_failure"]["kind"] == "lifecycle_interrupt"
            assert saved["failure_streak"] == streak
            assert saved.get("last_error_kind") == kind
            assert jobs.mark_job_run(job["id"], False, "real agent failure")
            saved = jobs.load_jobs()[0]
            assert saved["failure_streak"] == streak + 1
            assert saved["last_error_kind"] == "agent"
            assert "kind" not in saved["last_failure"], "a real failure replaces the typed interrupt"

    def test_no_op_when_nothing_running(self):
        import cron.scheduler as sched

        with patch("cron.scheduler.mark_job_run") as mock_mark:
            marked = sched.mark_running_jobs_interrupted("shutdown")

        assert marked == []
        mock_mark.assert_not_called()

    def test_marks_every_in_flight_job(self):
        import cron.scheduler as sched

        sched._running_job_ids.update(
            {sched._inflight_key("job-1"), sched._inflight_key("job-2")})
        profile_home = sched._get_hermes_home().resolve()
        sched._running_fire_owners.update(
            {
                sched._inflight_key("job-1"): {object(): ("owner-1", profile_home)},
                sched._inflight_key("job-2"): {object(): ("owner-2", profile_home)},
            }
        )

        with patch("cron.scheduler.mark_job_run", return_value=True) as mock_mark:
            marked = sched.mark_running_jobs_interrupted("gateway shutdown (final-cleanup)")

        assert sorted(marked) == ["job-1", "job-2"]
        assert mock_mark.call_count == 2
        called_ids = {c.args[0] for c in mock_mark.call_args_list}
        assert called_ids == {"job-1", "job-2"}
        for c in mock_mark.call_args_list:
            # success must be False -- an interrupted run is never "ok".
            assert c.args[1] is False
            assert "gateway shutdown" in c.args[2]
            assert c.kwargs["expected_fire_owner"] in {"owner-1", "owner-2"}

    def test_sets_interrupted_flag_for_consumption_by_run_one_job(self):
        import cron.scheduler as sched

        sched._running_job_ids.add(sched._inflight_key("job-1"))

        with patch("cron.scheduler.mark_job_run"):
            sched.mark_running_jobs_interrupted("shutdown")

        assert sched._inflight_key("job-1") in sched._interrupted_job_ids

    def test_one_job_marking_failure_does_not_block_the_others(self):
        """mark_job_run raising for one job (e.g. a jobs.json write race)
        must not prevent the rest from being marked -- this runs during
        shutdown, there's no retry window."""
        import cron.scheduler as sched

        sched._running_job_ids.update(
            {sched._inflight_key("job-1"), sched._inflight_key("job-2")})
        profile_home = sched._get_hermes_home().resolve()
        sched._running_fire_owners.update(
            {
                sched._inflight_key("job-1"): {object(): ("owner-1", profile_home)},
                sched._inflight_key("job-2"): {object(): ("owner-2", profile_home)},
            }
        )

        def _side_effect(job_id, success, reason, **kwargs):
            if job_id == "job-1":
                raise OSError("disk full")
            return True

        with patch("cron.scheduler.mark_job_run", side_effect=_side_effect):
            marked = sched.mark_running_jobs_interrupted("shutdown")

        assert marked == ["job-2"]

    def test_stale_shutdown_cannot_clear_replacement_owner(self, tmp_path):
        from cron import jobs
        import cron.scheduler as sched

        profile_home = tmp_path / "profile"
        profile_home.mkdir()
        with jobs.use_cron_store(profile_home):
            created = jobs.create_job(prompt="x", schedule="every 5m", name="owned")
            claimed = jobs.claim_job_for_fire(created["id"], force=True, return_job=True)
            assert isinstance(claimed, dict)
            stale_owner = claimed["fire_claim"]["by"]
            original_status = claimed["last_status"]
            replacement_claim = {
                "at": "2026-07-12T12:30:00+00:00",
                "by": "replacement-owner",
            }
            replacement = {**claimed, "fire_claim": replacement_claim}
            jobs.save_jobs([replacement])

            sched._running_job_ids.add(sched._inflight_key(created["id"]))
            sched._running_fire_owners[sched._inflight_key(created["id"])] = {
                object(): (stale_owner, profile_home)
            }
            marked = sched.mark_running_jobs_interrupted("shutdown")
            refreshed = jobs.get_job(created["id"])

        assert marked == []
        assert isinstance(refreshed, dict)
        assert refreshed["fire_claim"] == replacement_claim
        assert refreshed["last_status"] == original_status


class TestRunningFireOwnerRegistry:
    def test_run_one_job_registers_owner_only_while_active(self):
        import cron.scheduler as sched

        job = {
            "id": "owned-job",
            "fire_claim": {"at": "2026-07-12T12:00:00+00:00", "by": "owner-1"},
        }

        def _observe_registry(current_job, run, **_kw):
            assert list(sched._running_fire_owners[sched._inflight_key(current_job["id"])].values()) == [
                ("owner-1", sched._get_hermes_home().resolve())
            ]
            return True

        with patch("cron.scheduler._run_with_fire_claim_heartbeat", side_effect=_observe_registry):
            assert sched.run_one_job(job) is True

        assert sched._inflight_key(job["id"]) not in sched._running_fire_owners

    def test_shutdown_sees_all_concurrent_direct_fire_owners(self, monkeypatch):
        """Direct entry points and replacement owners share one token registry."""
        import cron.scheduler as sched

        entered = threading.Barrier(3)
        release = threading.Event()
        marked_owners: list[str] = []

        def hold_run(_job, _run, **_kw):
            entered.wait(timeout=2)
            release.wait(timeout=2)
            return True

        def mark(_job_id, _success, _reason, *, expected_fire_owner, shutdown_interrupted):
            assert shutdown_interrupted is True
            marked_owners.append(expected_fire_owner)
            return True

        monkeypatch.setattr(sched, "_run_with_fire_claim_heartbeat", hold_run)
        monkeypatch.setattr(sched, "mark_job_run", mark)

        jobs = [
            {"id": "same-job", "fire_claim": {"by": "old-owner"}},
            {"id": "same-job", "fire_claim": {"by": "replacement-owner"}},
        ]
        threads = [threading.Thread(target=sched.run_one_job, args=(job,)) for job in jobs]
        for thread in threads:
            thread.start()
        entered.wait(timeout=2)

        assert sched.get_running_job_ids() == frozenset({"same-job"})
        assert sched.mark_running_jobs_interrupted("shutdown") == ["same-job", "same-job"]
        assert set(marked_owners) == {"old-owner", "replacement-owner"}

        release.set()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        assert "same-job" not in sched.get_running_job_ids()

    def test_shutdown_marks_each_owner_in_its_profile_store(self, monkeypatch, tmp_path):
        import cron.jobs as cron_jobs
        import cron.scheduler as sched

        profile_a = tmp_path / "a"
        profile_b = tmp_path / "b"
        observed = []
        sched._running_fire_owners[sched._inflight_key("same-job")] = {
            object(): ("owner-a", profile_a),
            object(): ("owner-b", profile_b),
        }

        def mark(job_id, success, reason, *, expected_fire_owner, shutdown_interrupted):
            assert shutdown_interrupted is True
            observed.append(
                (
                    job_id,
                    success,
                    expected_fire_owner,
                    cron_jobs._current_cron_store().jobs_file,
                )
            )
            return True

        monkeypatch.setattr(sched, "mark_job_run", mark)

        assert sched.mark_running_jobs_interrupted("shutdown") == ["same-job", "same-job"]
        assert set(observed) == {
            ("same-job", False, "owner-a", profile_a / "cron" / "jobs.json"),
            ("same-job", False, "owner-b", profile_b / "cron" / "jobs.json"),
        }


class TestIsInterrupted:
    """Peek-only check used at the delivery gate -- must NOT clear the
    flag, unlike _consume_interrupted_flag."""



    def test_does_not_clear_the_flag(self):
        import cron.scheduler as sched

        sched._interrupted_job_ids.add(sched._inflight_key("job-1"))

        sched._is_interrupted("job-1")

        # Still set -- the later, authoritative check before mark_job_run
        # must still see it.
        assert sched._inflight_key("job-1") in sched._interrupted_job_ids
        assert sched._is_interrupted("job-1") is True


class TestConsumeInterruptedFlag:

    def test_true_and_clears_when_marked(self):
        import cron.scheduler as sched

        sched._interrupted_job_ids.add(sched._inflight_key("job-1"))

        assert sched._consume_interrupted_flag("job-1") is True
        # Consumed -- a second check (e.g. a later, unrelated fire of the
        # same recurring job ID) must not still read as interrupted.
        assert sched._consume_interrupted_flag("job-1") is False


class TestExecutionScopedInterruption:
    """Interruption flags must target ONE execution, not the job ID.

    Owner-registered executions are recorded by their unique execution
    token, so a fresh run that reuses the same job ID (recurring fire,
    replacement claim owner) never consumes a flag that targeted its
    dead predecessor.
    """

    def test_interruption_targets_only_the_interrupted_execution(self):
        import cron.scheduler as sched

        profile_home = sched._get_hermes_home().resolve()
        old_token = object()
        sched._running_fire_owners[sched._inflight_key("job-1")] = {
            old_token: ("owner-1", profile_home),
        }

        with patch("cron.scheduler.mark_job_run", return_value=True):
            sched.mark_running_jobs_interrupted("shutdown")

        assert sched._is_interrupted("job-1", old_token) is True
        new_token = object()
        assert sched._is_interrupted("job-1", new_token) is False
        # A new execution must not steal (and thereby clear) the old flag.
        assert sched._consume_interrupted_flag("job-1", new_token) is False
        assert sched._consume_interrupted_flag("job-1", old_token) is True
        assert sched._is_interrupted("job-1", old_token) is False

    def test_only_owners_marks_only_targeted_executions(self):
        import cron.scheduler as sched

        profile_home = sched._get_hermes_home().resolve()
        token_a, token_b = object(), object()
        sched._running_fire_owners[sched._inflight_key("job-a")] = {token_a: ("owner-a", profile_home)}
        sched._running_fire_owners[sched._inflight_key("job-b")] = {token_b: ("owner-b", profile_home)}

        with patch("cron.scheduler.mark_job_run", return_value=True) as mock_mark:
            marked = sched.mark_running_jobs_interrupted(
                "dashboard shutdown",
                only_owners={("job-a", "owner-a")},
            )

        assert marked == ["job-a"]
        assert mock_mark.call_count == 1
        assert mock_mark.call_args.kwargs["expected_fire_owner"] == "owner-a"
        assert sched._is_interrupted("job-a", token_a) is True
        assert sched._is_interrupted("job-b", token_b) is False

    def test_replacement_execution_of_same_job_is_not_poisoned(self):
        """A replacement owner starting while the stale flag exists must
        complete through the normal mark path, not the interrupted one."""
        import cron.scheduler as sched

        profile_home = sched._get_hermes_home().resolve()
        stale_token = object()
        sched._running_fire_owners[sched._inflight_key("job-1")] = {
            stale_token: ("stale-owner", profile_home),
        }
        with patch("cron.scheduler.mark_job_run", return_value=True):
            sched.mark_running_jobs_interrupted("shutdown")
        sched._running_fire_owners.clear()

        job = {
            "id": "job-1",
            "name": "test job",
            "prompt": "do work",
            "fire_claim": {"by": "replacement-owner"},
        }
        with patch("cron.scheduler.claim_dispatch", return_value=True), \
             patch("agent.secret_scope.set_secret_scope", return_value=None), \
             patch("agent.secret_scope.build_profile_secret_scope", return_value={}), \
             patch("agent.secret_scope.reset_secret_scope"), \
             patch(
                 "cron.scheduler.run_job",
                 return_value=(True, "full output", "final response", None),
             ), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._is_cron_silence_response", return_value=False), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.fire_claim_fence"), \
             patch("cron.scheduler.heartbeat_fire_claim", return_value=True), \
             patch("cron.scheduler.mark_job_run", return_value=True) as mock_mark:
            result = sched.run_one_job(job)

        assert result is True
        mock_mark.assert_called_once()


class TestCombinedCancelEvent:
    def test_or_semantics(self):
        import cron.scheduler as sched

        a, b = threading.Event(), threading.Event()
        combined = sched._CombinedCancelEvent(a, b)
        assert combined.is_set() is False
        b.set()
        assert combined.is_set() is True

    def test_set_propagates_to_all(self):
        import cron.scheduler as sched

        a, b = threading.Event(), threading.Event()
        combined = sched._CombinedCancelEvent(a, b)
        combined.set()
        assert a.is_set() and b.is_set()

    def test_run_one_job_forwards_external_cancel_event(self):
        import cron.scheduler as sched

        external = threading.Event()
        job = {"id": "job-x", "name": "x", "prompt": "p"}

        with patch.object(
            sched,
            "_run_with_fire_claim_heartbeat",
            side_effect=lambda job_arg, run, **_kw: run(threading.Event()),
        ), patch.object(sched, "_run_one_job_body", return_value=True) as body:
            assert sched.run_one_job(job, cancel_event=external) is True

        kwargs = body.call_args.kwargs
        assert kwargs["transport_cancel"] is external
        # What run_job receives: the ownership handle built from the two forwarded sources.
        combined = sched._FireOwnership(job, kwargs["claim_lost"], kwargs["transport_cancel"]).cancel_event
        assert combined.is_set() is False
        external.set()
        assert combined.is_set() is True


class TestBaseExceptionThroughOwnerFencedFlow:
    """#73973 (sweeper review on #70638): a BaseException escaping run_job
    must still record a failed run through the owner-fenced terminal path —
    and a stale worker must not record over a replacement claim owner."""

    def _job(self):
        return {
            "id": "job-be",
            "name": "base exc",
            "prompt": "p",
            "fire_claim": {"by": "owner-be"},
        }

    def _patches(self, run_side_effect):
        return (
            patch("cron.scheduler.claim_dispatch", return_value=True),
            patch("agent.secret_scope.set_secret_scope", return_value=None),
            patch("agent.secret_scope.build_profile_secret_scope", return_value={}),
            patch("agent.secret_scope.reset_secret_scope"),
            patch("cron.scheduler.run_job", side_effect=run_side_effect),
            patch("cron.scheduler.heartbeat_fire_claim", return_value=True),
        )

    def test_cancelled_error_records_failure_and_reraises(self):
        import asyncio

        import cron.scheduler as sched

        p1, p2, p3, p4, p5, p6 = self._patches(asyncio.CancelledError())
        with p1, p2, p3, p4, p5, p6, \
             patch("cron.scheduler.mark_job_run", return_value=True) as mock_mark, \
             patch("cron.scheduler.finish_execution") as mock_finish:
            try:
                sched.run_one_job(self._job())
                raised = False
            except asyncio.CancelledError:
                raised = True

        assert raised, "non-Exception BaseException must propagate"
        mock_mark.assert_called_once()
        assert mock_mark.call_args.args[:3] == ("job-be", False, "CancelledError")
        assert mock_mark.call_args.kwargs["expected_fire_owner"] == "owner-be"
        assert mock_finish.call_args.kwargs["success"] is False

    def test_keyboard_interrupt_records_failure_and_reraises(self):
        import cron.scheduler as sched

        p1, p2, p3, p4, p5, p6 = self._patches(KeyboardInterrupt())
        with p1, p2, p3, p4, p5, p6, \
             patch("cron.scheduler.mark_job_run", return_value=True) as mock_mark, \
             patch("cron.scheduler.finish_execution"):
            try:
                sched.run_one_job(self._job())
                raised = False
            except KeyboardInterrupt:
                raised = True

        assert raised
        mock_mark.assert_called_once()
        assert mock_mark.call_args.kwargs["expected_fire_owner"] == "owner-be"

    def test_base_exception_from_stale_owner_is_fenced_out(self):
        """A replacement owner reclaimed the job: the stale worker's
        BaseException path must NOT write terminal state over it."""
        import asyncio

        import cron.scheduler as sched

        p1, p2, p3, p4, p5, p6 = self._patches(asyncio.CancelledError())
        with p1, p2, p3, p4, p5, p6, \
             patch("cron.scheduler.mark_job_run", return_value=False) as mock_mark, \
             patch("cron.scheduler.finish_execution"):
            try:
                sched.run_one_job(self._job())
            except asyncio.CancelledError:
                pass

        mock_mark.assert_called_once()
        # fenced write was attempted with the stale owner and discarded by
        # the store (return False) — and the code accepted that verdict
        # without retrying or writing anything else.
        assert mock_mark.call_args.kwargs["expected_fire_owner"] == "owner-be"


class TestCallerLossAfterClaimAcquisition:
    """cirwel's integration assertion on #70638: if the HTTP/CLI caller is
    lost AFTER the claim was acquired, the gateway owner must produce at
    most one terminal ledger/artifact/delivery, clear only its own claim,
    and block retries while that ownership is live."""

    def test_second_fire_cannot_claim_while_first_ownership_live(self, tmp_path):
        from cron import jobs

        with jobs.use_cron_store(tmp_path):
            job = jobs.create_job(prompt="x", schedule="every 5m", name="owned")
            claimed = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
            assert isinstance(claimed, dict)

            # Caller died here — the claim outlives it. A retry (NAS/webhook
            # or manual) must be refused while the lease is fresh.
            retry = jobs.claim_job_for_fire(job["id"], return_job=True)
            assert retry is False or not isinstance(retry, dict)

            # The live owner still heartbeats and terminally marks — exactly
            # one terminal write, and only its own claim is cleared.
            owner = claimed["fire_claim"]["by"]
            assert jobs.heartbeat_fire_claim(job["id"], expected_owner=owner) is True
            assert jobs.mark_job_run(
                job["id"], True, expected_fire_owner=owner,
            ) is True
            refreshed = jobs.get_job(job["id"])
            assert refreshed["fire_claim"] is None
            assert refreshed["last_status"] == "ok"


class TestRunOneJobHonoursInterruptedFlag:
    """run_one_job() must not let a job's own completion overwrite a
    status the shutdown path already wrote for the same run."""

    def _make_job(self, job_id="job-1"):
        return {"id": job_id, "name": "test job", "prompt": "do work"}

    def test_success_path_skipped_when_interrupted(self):
        import cron.scheduler as sched

        job = self._make_job()
        sched._interrupted_job_ids.add(sched._inflight_key(job["id"]))

        with patch("cron.scheduler.claim_dispatch", return_value=True), \
             patch("agent.secret_scope.set_secret_scope", return_value=None), \
             patch("agent.secret_scope.build_profile_secret_scope", return_value={}), \
             patch("agent.secret_scope.reset_secret_scope"), \
             patch(
                 "cron.scheduler.run_job",
                 return_value=(True, "full output", "final response", None),
             ), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch("cron.scheduler._is_cron_silence_response", return_value=False), \
             patch("cron.scheduler._deliver_result", return_value=None), \
             patch("cron.scheduler.mark_job_run") as mock_mark:
            result = sched.run_one_job(job)

        assert result is True
        # The would-be "success" write must NOT happen -- the shutdown
        # path already wrote the authoritative interrupted status.
        mock_mark.assert_not_called()
        # Flag is consumed so a later, unrelated fire of the same job ID
        # isn't permanently silenced.
        assert sched._inflight_key(job["id"]) not in sched._interrupted_job_ids

    def test_interrupted_job_delivers_failure_summary_not_raw_response(self):
        """The status-write guard alone isn't enough: delivery happens
        BEFORE mark_job_run in run_one_job's own flow, so a job that kept
        running post-kill and produced a plausible-looking final_response
        must not have that response sent to the user just because the
        eventual status write gets suppressed. Interrupted jobs must route
        through the same failure-summary delivery path a real failure
        would."""
        import cron.scheduler as sched

        job = dict(self._make_job(), deliver="telegram")
        sched._interrupted_job_ids.add(sched._inflight_key(job["id"]))

        with patch("cron.scheduler.claim_dispatch", return_value=True), \
             patch("agent.secret_scope.set_secret_scope", return_value=None), \
             patch("agent.secret_scope.build_profile_secret_scope", return_value={}), \
             patch("agent.secret_scope.reset_secret_scope"), \
             patch(
                 "cron.scheduler.run_job",
                 return_value=(True, "full output", "a plausible final response", None),
             ), \
             patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
             patch(
                 "cron.scheduler._summarize_cron_failure_for_delivery",
                 return_value="This run was interrupted.",
             ) as mock_summarize, \
             patch("cron.scheduler._is_cron_silence_response", return_value=False), \
             patch("cron.delivery_queue.enqueue", return_value={"status": "pending"}) as mock_deliver, \
             patch("cron.scheduler.mark_job_run"):
            result = sched.run_one_job(job)

        assert result is True
        mock_summarize.assert_called_once()
        # The summarizer's error argument must mention the interruption,
        # not be silently None / the agent's own (possibly absent) error.
        assert "interrupt" in mock_summarize.call_args.args[1].lower()
        mock_deliver.assert_called_once()
        assert mock_deliver.call_args.kwargs["for_failure"] is True
        delivered_content = mock_deliver.call_args.args[2]
        assert delivered_content == "This run was interrupted."
        assert "plausible final response" not in delivered_content


    def test_exception_path_also_honours_interrupted_flag(self):
        import cron.scheduler as sched

        job = self._make_job()
        sched._interrupted_job_ids.add(sched._inflight_key(job["id"]))

        with patch("cron.scheduler.claim_dispatch", return_value=True), \
             patch("agent.secret_scope.set_secret_scope", return_value=None), \
             patch("agent.secret_scope.build_profile_secret_scope", return_value={}), \
             patch("agent.secret_scope.reset_secret_scope"), \
             patch("cron.scheduler.run_job", side_effect=RuntimeError("boom")), \
             patch("cron.scheduler.mark_job_run") as mock_mark:
            result = sched.run_one_job(job)

        assert result is False
        mock_mark.assert_not_called()


class TestRealHeartbeatThroughShutdownMark:
    """cron-7c04faf754: a gateway shutdown's ``mark_running_jobs_interrupted`` clears the fire claim
    of a run still in flight. The run's real fire-claim heartbeat then misses its claim and, before
    this fix, stopped the run as "cron fire ownership was lost" (ERROR + traceback) although no
    other process took the claim. These tests run the real heartbeat against the real store."""

    @pytest.fixture
    def fast_heartbeat(self, monkeypatch):
        import cron.scheduler as sched

        monkeypatch.setattr(sched, "_RUN_CLAIM_HEARTBEAT_SECONDS", 0.05)
        monkeypatch.setattr(sched, "_FIRE_CLAIM_MISS_CONFIRM_SECONDS", 0.01)
        # Run the body in-process: no restart-safe worker handoff.
        monkeypatch.setattr(sched, "_launch_external_cron_worker", lambda job: False)

    def _start_fire(self, monkeypatch):
        """Claim a real job and start run_one_job with a body that waits for the cancel latch."""
        import cron.scheduler as sched
        from cron import jobs
        from cron.cancellation import _CombinedCancelEvent, cancel_is_shutdown, cancel_reason

        job = jobs.create_job(prompt="x", schedule="every 5m", name="hb-shutdown")
        claimed = jobs.claim_job_for_fire(job["id"], force=True, return_job=True)
        assert isinstance(claimed, dict)
        started = threading.Event()
        seen: dict = {}

        def body(current_job, *, claim_lost, **_kw):
            started.set()
            seen["latched"] = claim_lost.wait(5)
            combined = _CombinedCancelEvent(claim_lost)
            seen["reason"] = cancel_reason(combined)
            seen["shutdown"] = cancel_is_shutdown(combined)
            return True

        monkeypatch.setattr(sched, "_run_one_job_body", body)
        thread = threading.Thread(target=sched.run_one_job, args=(claimed,))
        thread.start()
        assert started.wait(5)
        return claimed, thread, seen

    def test_shutdown_mark_stops_run_as_shutdown_not_lost_ownership(
            self, fast_heartbeat, monkeypatch, caplog):
        import logging

        import cron.scheduler as sched
        from cron import jobs

        caplog.set_level(logging.DEBUG, logger="cron.scheduler")
        claimed, thread, seen = self._start_fire(monkeypatch)
        shutdown_text = "Gateway shutdown (test) killed the job's tool subprocess before the run finished."
        assert sched.mark_running_jobs_interrupted(shutdown_text) == [claimed["id"]]
        thread.join(timeout=5)
        assert not thread.is_alive()

        assert seen["latched"] is True
        assert "shutdown" in seen["reason"].lower()
        assert "ownership was lost" not in seen["reason"]
        assert seen["shutdown"] is True
        assert not [r for r in caplog.records if "ownership lost" in r.getMessage()
                    or "ownership was lost" in r.getMessage()]
        assert [r for r in caplog.records if r.levelno == logging.WARNING
                and "shutdown interrupted" in r.getMessage().lower()]
        stored = jobs.get_job(claimed["id"])
        assert shutdown_text in (stored.get("last_error") or "")

    def test_foreign_takeover_still_reports_lost_ownership(
            self, fast_heartbeat, monkeypatch, caplog):
        import logging

        from cron import jobs

        caplog.set_level(logging.DEBUG, logger="cron.scheduler")
        claimed, thread, seen = self._start_fire(monkeypatch)
        # Another process takes the claim: rewrite fire_claim.by on disk.
        stored = jobs.load_jobs()
        for record in stored:
            if record["id"] == claimed["id"]:
                record["fire_claim"] = dict(record["fire_claim"], by="someone-else")
        jobs.save_jobs(stored)
        thread.join(timeout=5)
        assert not thread.is_alive()

        assert seen["latched"] is True
        assert seen["reason"] == "cron fire ownership was lost"
        assert seen["shutdown"] is False
        assert [r for r in caplog.records if r.levelno == logging.WARNING
                and "ownership lost" in r.getMessage()]


class TestOwnerSideShutdownCancel:
    """The owner gateway runs the agent (gateway.session_cron.execute); the firer forwards its
    latch as a reasonless ``cron.cancel``. When the owner is the same process whose shutdown
    marked the fire interrupted, the owner's run must report the shutdown, not lost ownership."""

    def _execute_with_cancel(self, monkeypatch, tmp_path, job_id):
        import asyncio
        import json
        from types import SimpleNamespace

        import cron.scheduler as sched
        from cron.cancellation import cancel_is_shutdown, cancel_reason
        from gateway import session_cron
        from gateway.session_contract import SessionRef
        from hermes_state_registry import acquire, release

        captured = {}

        def fake_run_job(job, *, cancel_event=None, **_kw):
            captured["reason"] = cancel_reason(cancel_event)
            captured["shutdown"] = cancel_is_shutdown(cancel_event)
            return True, "doc", "answer", None

        monkeypatch.setattr(sched, "run_job", fake_run_job)
        db = acquire(sched._get_hermes_home() / "state.db")
        ref = SessionRef("test-profile", "cron-owner-cancel")
        db.create_session(ref.session_id, source="cron")
        authority = SimpleNamespace(
            db=db, sessions={ref.session_id: SimpleNamespace(
                source=SimpleNamespace(user_id="cron-owner"))}, pending_results={})
        cancel = threading.Event()
        cancel.set()  # the firer's forwarded cron.cancel landed before the run checked it
        authority._cron_cancellations = {"adm": cancel}
        row = {"admission_id": "adm", "request_id": "req", "principal_id": "cron-owner",
               "owner_epoch": 1, "generation": 1, "payload": {"text": ""}}
        policy = SimpleNamespace(request_json=json.dumps({
            "cron_job": {"id": job_id, "prompt": "x"}, "extra_prompt": None, "request_id": "req"}))
        try:
            asyncio.run(session_cron.execute(authority, ref, row, policy))
        finally:
            release(db)
        return captured

    def test_owner_reports_shutdown_when_this_process_interrupted_the_fire(self, monkeypatch, tmp_path):
        import cron.scheduler as sched

        token = object()
        sched._running_fire_owners[sched._inflight_key("owner-job")] = {
            token: ("owner-1", sched._get_hermes_home().resolve())}
        sched._interrupted_job_ids.add(token)
        captured = self._execute_with_cancel(monkeypatch, tmp_path, "owner-job")
        assert "shutdown" in captured["reason"].lower()
        assert captured["shutdown"] is True

    def test_owner_keeps_lost_ownership_wording_without_shutdown(self, monkeypatch, tmp_path):
        captured = self._execute_with_cancel(monkeypatch, tmp_path, "owner-job")
        assert captured["reason"] == "cron fire ownership was lost"
        assert captured["shutdown"] is False


class TestOwnerStopWhileDraining:
    """cron-7c04faf754 recurrence: the owner gateway's own restart drain stops an agent cron run
    through ``stop_authority_work`` (a reasonless set of the run's cancel latch) while no scheduler
    ``mark_running_jobs_interrupted`` flag exists. That stop is this gateway's shutdown, so it must log
    one WARNING ``stopped by gateway shutdown`` (no ``Job '...' failed:`` ERROR) and still book the
    run as a restart cut. An operator Stop during the drain and a takeover without a drain keep their
    wording."""

    def _run(self, monkeypatch, caplog, *, draining, operator_cancel=False, drain_ends_before_poll=False):
        import asyncio
        import json
        import logging
        from types import SimpleNamespace

        import cron.scheduler as sched
        from cron.cancellation import (CronShutdownInterrupted, cancel_is_shutdown, cancel_reason,
                                       request_cancel)
        from gateway import session_cron
        from gateway.session_contract import SessionRef
        from gateway.session_runtime_workers import stop_authority_work
        from hermes_state_registry import acquire, release

        captured = {}
        holder = {}

        def fake_run_job(job, *, cancel_event=None, **_kw):
            # The drain timed out and stopped the profile's work mid-run (session_runtime_workers).
            if operator_cancel:
                request_cancel(job["execution_id"])
            stop_authority_work(holder["authority"])
            if drain_ends_before_poll:
                # _stop_persist_exit_state resets the drain flag before the run's next 5 s poll.
                holder["authority"].runner._draining = False
            assert cancel_event is not None and cancel_event.is_set()
            captured["reason"] = reason = cancel_reason(cancel_event)
            captured["shutdown"] = cancel_is_shutdown(cancel_event)
            # run_job's own stop + failure-log path (_abort_if_fire_claim_lost, run_job's except).
            error = CronShutdownInterrupted if cancel_is_shutdown(cancel_event) else RuntimeError
            exc = error(f"Cron job 'drain-job' was stopped: {reason}")
            if not sched._log_expected_stop("drain-job", str(exc), exc):
                try:
                    raise exc
                except RuntimeError:
                    sched.logger.exception("Job '%s' failed: %s", "drain-job", str(exc))
            return False, "doc", "", str(exc)

        caplog.set_level(logging.DEBUG, logger="cron.scheduler")
        monkeypatch.setattr(sched, "run_job", fake_run_job)
        monkeypatch.setattr("gateway.run_runtime.stop_authority_turns", lambda *a, **k: None)
        db = acquire(sched._get_hermes_home() / "state.db")
        ref = SessionRef("test-profile", "cron-owner-drain")
        db.create_session(ref.session_id, source="cron")
        authority = holder["authority"] = SimpleNamespace(
            db=db, sessions={ref.session_id: SimpleNamespace(
                source=SimpleNamespace(user_id="cron-owner"))}, pending_results={},
            runner=SimpleNamespace(_draining=draining, _interrupting_remaining_work=False))
        row = {"admission_id": "adm", "request_id": "req-drain", "principal_id": "cron-owner",
               "payload": {"text": ""}, "owner_epoch": 1, "generation": 1}
        policy = SimpleNamespace(request_json=json.dumps({
            "cron_job": {"id": "drain-job", "prompt": "x"}, "extra_prompt": None,
            "request_id": "req-drain"}))
        try:
            with pytest.raises(RuntimeError):
                asyncio.run(session_cron.execute(authority, ref, row, policy))
        finally:
            release(db)
        captured["flags"] = authority.pending_results["adm"]["result"]["cron_job_flags"]
        captured["records"] = [(r.levelno, r.getMessage(), r.exc_info is not None)
                               for r in caplog.records if r.name == "cron.scheduler"]
        return captured

    def test_drain_stop_logs_shutdown_warning_not_failed_error(self, monkeypatch, caplog):
        import logging

        from cron.restart_cut import RESTART_CUT_FLAG

        got = self._run(monkeypatch, caplog, draining=True)
        assert "shutdown" in got["reason"].lower(), got["reason"]
        assert got["shutdown"] is True
        assert [(lvl, tb) for lvl, _msg, tb in got["records"]] == [(logging.WARNING, False)]
        assert "stopped by gateway shutdown" in got["records"][0][1]
        assert not any("failed:" in msg for _lvl, msg, _tb in got["records"])
        assert got["flags"].get(RESTART_CUT_FLAG) is True

    def test_drain_stop_read_after_drain_flag_reset_is_still_a_shutdown(self, monkeypatch, caplog):
        """Live hub 2026-11-12: the stop landed while draining, but the run read its latch only after
        ``_stop_persist_exit_state`` reset ``_draining``; the shutdown fact is latched at stop time."""
        import logging

        from cron.restart_cut import RESTART_CUT_FLAG

        got = self._run(monkeypatch, caplog, draining=True, drain_ends_before_poll=True)
        assert got["shutdown"] is True, got["reason"]
        assert [(lvl, tb) for lvl, _msg, tb in got["records"]] == [(logging.WARNING, False)]
        assert "stopped by gateway shutdown" in got["records"][0][1]
        assert not any("failed:" in msg for _lvl, msg, _tb in got["records"])
        assert got["flags"].get(RESTART_CUT_FLAG) is True

    def test_operator_cancel_during_drain_keeps_cancelled_on_request(self, monkeypatch, caplog):
        got = self._run(monkeypatch, caplog, draining=True, operator_cancel=True)
        assert got["shutdown"] is False
        assert "shutdown" not in got["reason"].lower()
        assert got["reason"] != "cron fire ownership was lost"

    def test_takeover_without_drain_keeps_lost_ownership_error(self, monkeypatch, caplog):
        import logging

        got = self._run(monkeypatch, caplog, draining=False)
        assert got["reason"] == "cron fire ownership was lost"
        assert got["shutdown"] is False
        assert any(lvl == logging.ERROR and "failed:" in msg and tb
                   for lvl, msg, tb in got["records"])


class TestShutdownStopLogging:
    def test_watchdog_raises_shutdown_interrupt_for_a_shutdown_latch(self):
        import cron.scheduler as sched
        from cron.cancellation import CronShutdownInterrupted, SHUTDOWN_INTERRUPT_REASON

        latch = threading.Event()
        latch.reason = SHUTDOWN_INTERRUPT_REASON
        latch.shutdown_interrupted = True
        latch.set()

        class Agent:
            def run_conversation(self, prompt, task_id=None):
                return {"final_response": "late"}

            def interrupt(self, message):
                pass

        with pytest.raises(CronShutdownInterrupted) as raised:
            sched._run_agent_with_watchdog(
                Agent(), "p", {"schedule": {"kind": "every"}}, "j", "job", "t", latch)
        assert SHUTDOWN_INTERRUPT_REASON in str(raised.value)

    def test_shutdown_stop_logs_one_warning_without_traceback(self, caplog):
        import logging

        import cron.scheduler as sched
        from cron.cancellation import CronShutdownInterrupted

        caplog.set_level(logging.DEBUG, logger="cron.scheduler")
        assert sched._log_expected_stop("job", "stopped", CronShutdownInterrupted("stopped")) is True
        records = [r for r in caplog.records if r.name == "cron.scheduler"]
        assert [(r.levelno, r.exc_info) for r in records] == [(logging.WARNING, None)]
        assert not records[0].getMessage().startswith("Job 'job' failed: ")

    def test_other_failures_keep_run_jobs_error_traceback(self, caplog):
        """Real ownership loss (plain RuntimeError) is not claimed: run_job's logger.exception runs."""
        import logging

        import cron.scheduler as sched

        caplog.set_level(logging.DEBUG, logger="cron.scheduler")
        exc = RuntimeError("Cron job 'job' was stopped: cron fire ownership was lost")
        assert sched._log_expected_stop("job", str(exc), exc) is False
        assert [r for r in caplog.records if r.name == "cron.scheduler"] == []
