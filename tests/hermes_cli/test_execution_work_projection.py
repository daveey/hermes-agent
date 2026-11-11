"""Work semantics layered on the published v1 capacity observation envelope."""
from copy import deepcopy

import pytest

from hermes_cli import execution_observations as obs


def record(state="running"):
    return {"version": 1, "identity": {"gateway": "edge", "profile": "work", "board": "tasks",
            "kind": "card", "work_id": "task-a", "run_id": 7},
            "owner": {"gateway": "edge", "profile": "work", "assignee": "builder"},
            "state": state, "source": "task_run", "source_revision": "event:42",
            "observed_at": 1000, "valid_until": 1600, "evidence_refs": ["event:42"],
            "last_activity": 1000, "last_activity_kind": "heartbeat"}


def wait():
    return {"kind": "dependency", "target": {"gateway": "edge", "profile": "work", "board": "tasks",
            "kind": "card", "work_id": "parent-a"}, "predicate": "status=done", "since": 990,
            "due_at": None, "owner": {"gateway": "edge", "profile": "work"}}


def test_freshness_is_source_owned_and_projection_does_not_mutate():
    value = record()
    before = deepcopy(value)
    assert obs.project(value, now=1179, source_class="sidebar")["state"] == "running"
    assert obs.project(value, now=1180, source_class="sidebar")["state"] == "unknown"
    assert obs.project(value, now=1599, source_class="imported_owner")["state"] == "running"
    assert obs.project(value, now=1600, source_class="imported_owner")["state"] == "unknown"
    assert value == before


@pytest.mark.parametrize("state", ["waiting", "needs_user"])
def test_wait_needs_exact_condition_and_accountable_owner(state):
    value = record(state)
    assert obs.project(value, now=1001)["state"] == "unknown"
    value["wait"] = wait()
    if state == "needs_user":
        # Decision: a human request cannot accompany a dependency/time/transient wait.
        assert obs.project({**value, "human_request": {"action": "x", "why_human": "x", "interaction": "reply",
                           "scope": "x", "evidence": "x"}}, now=1001)["unknown_reason"] == "invalid_human_wait"
        value["wait"].update(kind="human", predicate="request_answered")
        value["human_request"] = {"action": "Choose destination", "why_human": "User choice",
                                  "interaction": "reply", "scope": "this task", "evidence": "two destinations"}
    assert obs.project(value, now=1001)["state"] == state
    for field in ("target", "predicate", "owner", "since"):
        bad = deepcopy(value)
        bad["wait"].pop(field)
        assert obs.project(bad, now=1001)["state"] == "unknown"


def test_observation_never_authorizes_an_action_and_retains_request_on_failure():
    value = record("needs_user")
    value["wait"] = wait()
    assert obs.project(value, now=1001)["unknown_reason"] == "missing_human_request"
    value["human_request"] = {"action": "Select A", "why_human": "Choice", "interaction": "reply",
                              "scope": "task", "evidence": "A or B"}
    value["version"] = 42
    result = obs.project(value, now=1001)
    assert result["state"] == "unknown"
    assert result["human_request"] == value["human_request"]
    assert "authorized" not in result


@pytest.mark.parametrize("kind", ["heartbeat", "poll", "claimed", "commented", None])
def test_activity_cannot_manufacture_progress(kind):
    value = record()
    value.update(last_progress=1000, last_progress_kind=kind, last_progress_ref="event:42")
    result = obs.project(value, now=1001)
    assert result["last_activity"] == 1000
    assert result["last_progress"] is None
    assert result["state"] == "running"


def test_explicit_progress_must_have_reference_and_successful_evidence():
    value = record()
    value.update(last_progress=999, last_progress_kind="test_result", last_progress_ref="test:abc",
                 progress_evidence={"successful": True, "evidence_ref": "test:abc", "observed_at": 999})
    assert obs.project(value, now=1001)["last_progress"] == 999
    value["progress_evidence"]["successful"] = False
    assert obs.project(value, now=1001)["last_progress"] is None


def test_done_needs_matching_verified_receipt_and_expiry_retains_history():
    value = record("done")
    assert obs.project(value, now=1001)["state"] == "unknown"
    value["delivery_receipt"] = {"verified": True, "identity": deepcopy(value["identity"]),
                                 "disposition": "delivered", "evidence_ref": "completed:42"}
    assert obs.project(value, now=1001)["state"] == "done"
    expired = obs.project(value, now=1180)
    assert expired["state"] == "unknown"
    assert expired["delivery_receipt"] == value["delivery_receipt"]
    for disposition in ("archived", "cancelled", "contained", "superseded", "rejected"):
        value["delivery_receipt"]["disposition"] = disposition
        assert obs.project(value, now=1001)["state"] == "unknown"


@pytest.mark.parametrize("field,value", [("gateway", "elsewhere"), ("profile", "other"),
                                         ("board", "other"), ("run_id", 8), ("work_id", "task-b")])
def test_execution_identity_cannot_alias(field, value):
    raw = record()
    expected = {**raw["identity"], field: value}
    assert obs.project(raw, now=1001, expected_identity=expected)["state"] == "unknown"


def test_out_of_order_and_run_rollover_are_fenced_by_current_identity():
    current = record()
    older = record("queued")
    older.update(observed_at=999, source_revision="event:41")
    assert obs.select([current, older], identity=current["identity"], now=1001)["state"] == "running"
    next_run = record("queued")
    next_run["identity"]["run_id"] = 8
    assert obs.select([current, next_run], identity=next_run["identity"], now=1001)["state"] == "queued"
    assert obs.select([current], identity=next_run["identity"], now=1001)["state"] == "unknown"


@pytest.mark.parametrize("clock", [float("nan"), float("inf"), -1, True, "1000", 1002])
def test_bad_source_clock_is_unknown(clock):
    value = record()
    value["observed_at"] = clock
    assert obs.project(value, now=1001)["state"] == "unknown"


def test_partial_source_unknown_cannot_be_masked_by_fresh_running():
    raw = record()
    unknown = {**record(), "state": "unknown", "unknown_reason": "source_offline"}
    unknown["identity"] = {**raw["identity"], "work_id": "task-b"}
    result = obs.summarize([raw, unknown], now=1001)
    assert result["counts"]["running"] == 1
    assert result["counts"]["unknown"] == 1
    assert result["uncertain"] is True
    assert obs.summarize([], now=1001)["uncertain"] is True
