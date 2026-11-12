"""Latency is receipt evidence, never readiness inference or liveness."""
import pytest

from hermes_cli.kanban_capacity_metrics import attempts, idle_coverage, summarize


def event(n, kind, at, run=None, **payload):
    return {"id": n, "kind": kind, "created_at": at, "run_id": run, "payload": payload}


def history():
    return [event(1, "promoted", 10), event(2, "claimed", 12, 7, lock="host:1"),
            event(3, "spawned", 13, 7, pid=111),
            event(4, "worker_bound", 15, 7, pid=222, claim_lock="host:1", started_at="birth|2")]


def test_actual_interpreter_not_submitter_and_no_progress_inference():
    out = attempts(history(), board="one", task_id="t", now=20)
    assert len(out) == 1
    assert out[0]["ready_to_claim_s"] == 2
    assert out[0]["claim_to_process_start_s"] == 3
    assert out[0]["ready_to_process_start_s"] == 5
    assert out[0]["process_pid"] == 222
    assert out[0]["last_progress"] is None


def test_creation_time_and_claim_do_not_substitute_for_missing_receipts():
    events = [event(1, "created", 1, status="ready"), event(2, "claimed", 12, 7, lock="h")]
    out = attempts(events, board="one", task_id="t", now=20)[0]
    assert out["ready_to_claim_s"] is None
    assert out["claim_to_process_start_s"] is None
    assert out["censored"] is True
    assert "missing_ready_receipt" in out["unknown"]


@pytest.mark.parametrize("change", ["wrong_run", "wrong_lock", "missing_birth", "skew"])
def test_start_requires_matching_attempt_and_consistent_clock(change):
    events = history()
    if change == "wrong_run":
        events[-1]["run_id"] = 8
    elif change == "wrong_lock":
        events[-1]["payload"]["claim_lock"] = "other"
    elif change == "missing_birth":
        events[-1]["payload"].pop("started_at")
    else:
        events[-1]["created_at"] = 11
    out = attempts(events, board="one", task_id="t", now=20)[0]
    assert out["claim_to_process_start_s"] is None
    assert out["ready_to_process_start_s"] is None


def test_retry_does_not_reuse_previous_ready_or_process_start():
    events = history() + [event(5, "released", 16, 7, retry_status="ready"),
                          event(6, "claimed", 18, 8, lock="h:2")]
    out = attempts(events, board="one", task_id="t", now=20)
    assert len(out) == 2
    assert out[1]["ready_to_claim_s"] == 2
    assert out[1]["process_pid"] is None
    assert out[1]["censored"]


def test_dependency_and_observation_activity_do_not_establish_ready_or_progress():
    events = [event(1, "created", 1, status="todo"), event(2, "dependency_wait", 2),
              event(3, "heartbeat", 3), event(4, "commented", 4)]
    assert attempts(events, board="one", task_id="t", now=20) == []


def test_summary_keeps_pending_and_small_sample_percentiles_unknown():
    out = attempts(history(), board="one", task_id="t", now=20)
    out += attempts([event(1, "promoted", 16)], board="two", task_id="t", now=20)
    report = summarize(out)
    assert report["attempt_count"] == 2
    assert report["censored_count"] == 1
    assert report["claim_to_process_start_s"]["sample_count"] == 1
    assert report["claim_to_process_start_s"]["p50"] is None
    assert report["claim_to_process_start_s"]["samples"] == [3]


def interval(board="one", **overrides):
    return {"machine": "m", "slot_scope": "host", "board": board,
            "start": 10, "end": 20, "observed_at": 10, "valid_until": 20,
            "eligible_backlog": 2, "admissible_slots": 3, "restrictions": [],
            "coverage_complete": True, **overrides}


def test_idle_shared_slots_never_sum_across_boards():
    result = idle_coverage([interval(), interval("two")])
    assert result["unexplained_idle_slot_seconds"] == 20
    assert result["unknown_seconds"] == 0


@pytest.mark.parametrize("overrides", [
    {"observed_at": None}, {"valid_until": 15}, {"eligible_backlog": None},
    {"admissible_slots": None}, {"coverage_complete": False}, {"observed_at": 11},
])
def test_missing_stale_skewed_telemetry_is_unknown_not_idle(overrides):
    result = idle_coverage([interval(**overrides)])
    assert result["unexplained_idle_slot_seconds"] == 0
    assert result["unknown_seconds"] == 10


def test_restrictions_and_no_backlog_are_not_unexplained_idle():
    for sample in [interval(restrictions=["capacity.memory_pressure"]), interval(eligible_backlog=0)]:
        result = idle_coverage([sample])
        assert result["unexplained_idle_slot_seconds"] == 0
        assert result["explained_seconds"] == 10


@pytest.mark.parametrize("kind", ["released", "reclaimed", "owner_unavailable", "crashed", "rate_limited",
                                  "timed_out", "stale", "reconciled", "owner_restart_lost",
                                  "protocol_violation", "oom_killed", "signaled"])
def test_explicit_retry_ready_receipt_is_required(kind):
    for payload, expected in (({}, None), ({"retry_status": "review"}, None),
                              ({"retry_status": "ready"}, 2)):
        rows = attempts([event(1, kind, 10, 7, **payload), event(2, "claimed", 12, 8, lock="h")],
                        board="one", task_id="t", now=20)
        assert rows[0]["ready_to_claim_s"] == expected


def test_summary_receipt_resolution_and_percentile_minima():
    rows = attempts(history(), board="one", task_id="t", now=20)
    assert summarize(rows)["receipt_resolution_seconds"] == 1
    assert summarize(rows * 2)["ready_to_claim_s"]["p50"] == 2
    assert summarize(rows * 19)["ready_to_claim_s"]["p95"] is None
    assert summarize(rows * 20)["ready_to_claim_s"]["p95"] == 2


@pytest.mark.parametrize("kind", ["status", "auto_blocked", "gave_up", "block_loop_detected",
                                  "agent_block_escalated", "supervisor_escalated", "continuation_exhausted"])
def test_leaving_ready_invalidates_old_receipt(kind):
    rows = attempts([event(1, "promoted", 1), event(2, kind, 2, status="triage"),
                     event(3, "claimed", 52, 1, lock="h")], board="one", task_id="t", now=60)
    assert rows[0]["ready_to_claim_s"] is None


@pytest.mark.parametrize("kind", ["promoted_manual", "unblocked"])
def test_native_manual_ready_receipts(kind):
    ready = event(3, kind, 50)
    if kind == "unblocked":
        ready["payload"] = None
    rows = attempts([event(1, "promoted", 1), event(2, "auto_blocked", 2), ready,
                     event(4, "claimed", 52, 1, lock="h")], board="one", task_id="t", now=60)
    assert rows[0]["ready_to_claim_s"] == 2


@pytest.mark.parametrize("swept", [False, True])
def test_import_invalidates_historical_ready_and_sweep_starts_new_interval(swept):
    events = [event(1, "promoted", 100), event(2, "imported", 200, parked=True)]
    if swept:
        events.append(event(3, "triage_swept", 5000, assignee="default"))
    events.append(event(4, "claimed", 5002, 1, lock="h"))
    rows = attempts(events, board="one", task_id="t", now=5010)
    assert rows[0]["ready_to_claim_s"] == (2 if swept else None)


def test_idle_is_capped_by_deduplicated_pool_backlog():
    result = idle_coverage([interval(eligible_backlog=1, admissible_slots=5)] * 2)
    assert result["unexplained_idle_slot_seconds"] == 10


def test_conflicting_pool_backlog_is_unknown_not_summed():
    result = idle_coverage([interval(), interval("two", eligible_backlog=1)])
    assert result["unknown_seconds"] == 10
    assert result["unexplained_idle_slot_seconds"] == 0


def test_conflicting_shared_slot_readings_are_unknown():
    result = idle_coverage([interval(), interval("two", admissible_slots=4)])
    assert result["unknown_seconds"] == 10
    assert result["unexplained_idle_slot_seconds"] == 0


def test_submitter_spawn_receipt_is_never_process_start_even_with_binding_fields():
    events = [event(1, "promoted", 10), event(2, "claimed", 12, 7, lock="host:1"),
              event(3, "spawned", 13, 7, pid=111, claim_lock="host:1", started_at="birth|1")]
    out = attempts(events, board="one", task_id="t", now=20)[0]
    assert out["process_start_at"] is None and out["process_pid"] is None
    assert out["claim_to_process_start_s"] is None and out["censored"] is True


def test_telemetry_gap_between_observations_is_unknown_not_dropped():
    result = idle_coverage([interval(start=10, end=20),
                            interval(start=30, end=40, observed_at=30, valid_until=40)])
    assert result["unknown_seconds"] == 10
    assert result["observed_seconds"] == 20
    assert result["unexplained_idle_slot_seconds"] == 40
