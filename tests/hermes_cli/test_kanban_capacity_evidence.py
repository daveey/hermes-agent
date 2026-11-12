"""Capacity evidence is concurrent, durable, lifecycle fenced and advisory only."""
from contextlib import closing
from dataclasses import asdict

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_capacity_evidence as evidence


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    assert kb.kanban_db_path().is_relative_to(tmp_path)
    with closing(kbc.connect()) as conn:
        yield conn


@pytest.mark.parametrize("field,value,code", [
    ("memory_pressure", "critical", "capacity.memory_pressure"),
    ("rate_limited", ["TASK"], "capacity.provider_limit"),
    ("skipped_ineligible", [("TASK", "labels_mismatch")], "capacity.placement"),
])
def test_native_dispatch_result_persists_without_mutation(board, field, value, code):
    tid = kb.create_task(board, title="one", assignee="alice")
    if isinstance(value, list):
        value = [tid if x == "TASK" else (tid, x[1]) for x in value]
    result = dispatch.DispatchResult(**{field: value})
    before = asdict(result)
    evidence.record(board, result, board="sample", machine="machine-a", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), board="sample", now=1001)
    assert code in [r["code"] for r in out["reasons"]]
    assert asdict(result) == before
    if field == "rate_limited":
        assert out["reasons"][0]["phase"] == "post_claim"


def test_multiple_reasons_dependency_and_update_drain(board):
    parent = kb.create_task(board, title="parent", assignee="alice")
    child = kb.create_task(board, title="child", assignee="alice", parents=[parent])
    result = dispatch.DispatchResult(memory_pressure="critical")
    evidence.record(board, result, board="sample", machine="machine-a", now=1000, update_drain=True)
    out = evidence.read(board, kb.get_task(board, child), board="sample", now=1001)
    assert {r["code"] for r in out["reasons"]} == {"capacity.dependency"}
    assert {r["code"] for r in out["machine_reasons"]} == {
        "capacity.memory_pressure", "capacity.update_drain"}
    ready = evidence.read(board, kb.get_task(board, parent), board="sample", now=1001)
    assert {r["code"] for r in ready["reasons"]} == {
        "capacity.memory_pressure", "capacity.update_drain"}
    assert out["eligible_backlog"] is False


def test_read_does_not_renew_timestamp_or_create_table(board):
    tid = kb.create_task(board, title="one", assignee="alice")
    task = kb.get_task(board, tid)
    assert evidence.read(board, task, board="sample", now=1000)["freshness"] == "unknown"
    assert not board.execute("SELECT name FROM sqlite_master WHERE name='kanban_capacity_observation'").fetchall()
    evidence.record(board, dispatch.DispatchResult(memory_pressure="critical"), board="sample", machine="m", now=1000)
    current = evidence.read(board, task, board="sample", now=1001)
    stale = evidence.read(board, task, board="sample", now=1180)
    assert current["observed_at_ms"] == stale["observed_at_ms"] == 1000000
    assert stale["freshness"] == "stale"
    assert stale["eligible_backlog"] is None
    assert stale["execution_observation"]["state"] == "unknown"


def test_retry_or_board_mismatch_cannot_reuse_restrictions(board):
    tid = kb.create_task(board, title="one", assignee="alice")
    evidence.record(board, dispatch.DispatchResult(memory_pressure="critical"), board="sample", machine="m", now=1000)
    assert evidence.read(board, kb.get_task(board, tid), board="other", now=1001)["freshness"] == "unknown"
    kb.claim_task(board, tid)
    out = evidence.read(board, kb.get_task(board, tid), board="sample", now=1001)
    assert out["freshness"] == "unknown" and out["unknown_reason"] == "lifecycle_changed"


def test_clock_skew_and_missing_admission_coverage_are_unknown(board):
    tid = kb.create_task(board, title="one", assignee="alice")
    evidence.record(board, dispatch.DispatchResult(), board="sample", machine="m", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), board="sample", now=999)
    assert out["freshness"] == "unknown"
    out = evidence.read(board, kb.get_task(board, tid), board="sample", now=1001)
    assert out["admissible_slots"] is None and out["eligible_backlog"] is None
    assert out["unknown_coverage"]


def test_dry_or_skipped_tick_never_manufactures_evidence(board):
    tid = kb.create_task(board, title="one", assignee="alice")
    for result in [dispatch.DispatchResult(skipped_locked=True), dispatch.DispatchResult(skipped_owned=True)]:
        assert evidence.record(board, result, board="sample", machine="m", now=1000) is False
    assert evidence.record(board, dispatch.DispatchResult(), board="sample", machine="m", now=1000, dry_run=True) is False
    assert evidence.read(board, kb.get_task(board, tid), board="sample", now=1001)["freshness"] == "unknown"


@pytest.mark.parametrize("native,is_provider", [
    ("subscription_quota_reset", True), ("rate_limit_cooldown", True),
    ("infrastructure_cooldown", False), ("owner_restart_cooldown", False),
])
def test_native_provider_clocks_are_preclaim_and_deduplicated(board, native, is_provider):
    tid = kb.create_task(board, title="clock", assignee="alice")
    result = dispatch.DispatchResult(clock_held={tid: (native, 1100)}, respawn_guarded=[(tid, native)])
    evidence.record(board, result, board="sample", machine="m", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), board="sample", now=1001)
    assert len(out["reasons"]) == int(is_provider)
    if is_provider:
        assert out["reasons"][0]["native_code"] == native
        assert out["reasons"][0]["phase"] == "pre_claim"


def test_claim_does_not_prove_execution_or_inherit_preclaim_pressure(board):
    tid = kb.create_task(board, title="claimed", assignee="alice")
    kb.claim_task(board, tid)
    evidence.record(board, dispatch.DispatchResult(memory_pressure="critical"),
                    board="sample", machine="m", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), board="sample", now=1001)
    assert out["reasons"] == []
    assert out["execution_observation"]["state"] == "unknown"
    assert out["execution_observation"]["last_progress"] is None


def test_read_never_invents_generation_from_an_event_or_poll(board):
    tid = kb.create_task(board, title="unclaimed", assignee="alice")
    evidence.record(board, dispatch.DispatchResult(), board="sample", machine="m", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), board="sample", now=1001)
    assert out["freshness"] == "current"
    observation = out["execution_observation"]
    assert "generation" not in observation["identity"]
    assert "run_id" not in observation["identity"]
    assert observation["state"] == "unknown"
