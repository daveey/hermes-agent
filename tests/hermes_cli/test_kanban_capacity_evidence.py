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
    evidence.record(board, result, board="default", machine="machine-a", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), board="default", now=1001)
    assert code in [r["code"] for r in out["reasons"]]
    assert asdict(result) == before
    if field == "rate_limited":
        assert out["reasons"][0]["phase"] == "post_claim"


def test_multiple_reasons_dependency_and_update_drain(board):
    parent = kb.create_task(board, title="parent", assignee="alice")
    child = kb.create_task(board, title="child", assignee="alice", parents=[parent])
    result = dispatch.DispatchResult(memory_pressure="critical")
    evidence.record(board, result, board="default", machine="machine-a", now=1000, update_drain=True)
    out = evidence.read(board, kb.get_task(board, child), board="default", now=1001)
    assert {r["code"] for r in out["reasons"]} == {"capacity.dependency"}
    assert {r["code"] for r in out["machine_reasons"]} == {
        "capacity.memory_pressure", "capacity.update_drain"}
    ready = evidence.read(board, kb.get_task(board, parent), board="default", now=1001)
    assert {r["code"] for r in ready["reasons"]} == {
        "capacity.memory_pressure", "capacity.update_drain"}
    assert out["eligible_backlog"] is False


def test_read_does_not_renew_timestamp_or_create_table(board):
    tid = kb.create_task(board, title="one", assignee="alice")
    task = kb.get_task(board, tid)
    assert evidence.read(board, task, board="default", now=1000)["freshness"] == "unknown"
    assert not board.execute("SELECT name FROM sqlite_master WHERE name='kanban_capacity_observation'").fetchall()
    evidence.record(board, dispatch.DispatchResult(memory_pressure="critical"), board="default", machine="m", now=1000)
    current = evidence.read(board, task, board="default", now=1001)
    stale = evidence.read(board, task, board="default", now=1180)
    assert current["observed_at_ms"] == stale["observed_at_ms"] == 1000000
    assert stale["freshness"] == "stale"
    assert stale["eligible_backlog"] is None
    assert stale["execution_observation"]["state"] == "unknown"


def test_retry_or_board_mismatch_cannot_reuse_restrictions(board):
    tid = kb.create_task(board, title="one", assignee="alice")
    evidence.record(board, dispatch.DispatchResult(memory_pressure="critical"), board="default", machine="m", now=1000)
    assert evidence.read(board, kb.get_task(board, tid), board="other", now=1001)["freshness"] == "unknown"
    kb.claim_task(board, tid)
    out = evidence.read(board, kb.get_task(board, tid), board="default", now=1001)
    assert out["freshness"] == "unknown" and out["unknown_reason"] == "lifecycle_changed"


def test_clock_skew_and_missing_admission_coverage_are_unknown(board):
    tid = kb.create_task(board, title="one", assignee="alice")
    evidence.record(board, dispatch.DispatchResult(), board="default", machine="m", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), board="default", now=999)
    assert out["freshness"] == "unknown"
    out = evidence.read(board, kb.get_task(board, tid), board="default", now=1001)
    assert out["admissible_slots"] is None and out["eligible_backlog"] is None
    assert out["unknown_coverage"]


def test_dry_or_skipped_tick_never_manufactures_evidence(board):
    tid = kb.create_task(board, title="one", assignee="alice")
    for result in [dispatch.DispatchResult(skipped_locked=True), dispatch.DispatchResult(skipped_owned=True)]:
        assert evidence.record(board, result, board="default", machine="m", now=1000) is False
    assert evidence.record(board, dispatch.DispatchResult(), board="default", machine="m", now=1000, dry_run=True) is False
    assert evidence.read(board, kb.get_task(board, tid), board="default", now=1001)["freshness"] == "unknown"


@pytest.mark.parametrize("native,is_provider", [
    ("subscription_quota_reset", True), ("rate_limit_cooldown", True),
    ("infrastructure_cooldown", False), ("owner_restart_cooldown", False),
])
def test_native_provider_clocks_are_preclaim_and_deduplicated(board, native, is_provider):
    tid = kb.create_task(board, title="clock", assignee="alice")
    result = dispatch.DispatchResult(clock_held={tid: (native, 1100)}, respawn_guarded=[(tid, native)])
    evidence.record(board, result, board="default", machine="m", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), board="default", now=1001)
    assert len(out["reasons"]) == int(is_provider)
    if is_provider:
        assert out["reasons"][0]["native_code"] == native
        assert out["reasons"][0]["phase"] == "pre_claim"


def test_claim_does_not_prove_execution_or_inherit_preclaim_pressure(board):
    tid = kb.create_task(board, title="claimed", assignee="alice")
    kb.claim_task(board, tid)
    evidence.record(board, dispatch.DispatchResult(memory_pressure="critical"),
                    board="default", machine="m", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), board="default", now=1001)
    assert out["reasons"] == []
    assert out["execution_observation"]["state"] == "unknown"
    assert out["execution_observation"]["last_progress"] is None


def test_parent_transition_invalidates_dependency_before_ttl(board):
    parent = kb.create_task(board, title="parent", assignee="alice")
    child = kb.create_task(board, title="child", assignee="alice", parents=[parent])
    evidence.record(board, dispatch.DispatchResult(), board="default", machine="m", now=1000)
    # Model the interval after a parent's terminal transition, before promotion.
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    out = evidence.read(board, kb.get_task(board, child), now=1001)
    assert out["freshness"] == "unknown" and out["unknown_reason"] == "lifecycle_changed"
    assert out["eligible_backlog"] is None


@pytest.mark.parametrize("field,value,native", [
    ("at_capacity", "max_spawn=3/3", "max_spawn=3/3"),
    ("claim_deferred", [("TASK", "lease_held")], "lease_held"),
    ("capsule_refused", [("TASK", "missing_capsule")], "missing_capsule"),
    ("owner_down", [("TASK", "alice", "owner_unavailable")], "owner_unavailable"),
    ("respawn_guarded", [("TASK", "recent_success")], "recent_success"),
])
def test_native_restriction_subcodes_retained(board, field, value, native):
    tid = kb.create_task(board, title="one", assignee="alice")
    if isinstance(value, list):
        value = [(tid, *item[1:]) for item in value]
    evidence.record(board, dispatch.DispatchResult(**{field: value}), board="default", machine="m", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), now=1001)
    assert any(r["native_code"] == native and r["source_field"] == field for r in out["native_restrictions"])


@pytest.mark.parametrize("native", ["labels_mismatch", "pinned_only", "offline"])
def test_board_claim_placement_is_namespaced(board, native):
    tid = kb.create_task(board, title="one", assignee="alice")
    evidence.record(board, dispatch.DispatchResult(claim_deferred=[(tid, native)]),
                    board="default", machine="m", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), now=1001)
    assert any(r["code"] == "capacity.placement" and r["native_code"] == native for r in out["reasons"])


def test_observer_uses_actual_nondefault_connection_not_argument(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    path = tmp_path / "kanban" / "boards" / "named" / "kanban.db"
    path.parent.mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    with closing(kbc.connect(path)) as conn:
        tid = kb.create_task(conn, title="one", assignee="alice")
        task = kb.get_task(conn, tid)
        for supplied in (None, "wrong"):
            evidence.observe_dispatch(conn, dispatch.DispatchResult(memory_pressure="critical"), board=supplied)
            out = evidence.read(conn, task)
            assert out["freshness"] == "current" and out["board"] == "named"
            assert evidence.read(conn, task, board="wrong")["freshness"] == "unknown"


@pytest.mark.parametrize("binding,expected", [("matching", "post_start"), ("wrong_lock", "post_claim"),
                                           ("missing", "post_claim"), ("prior_attempt", "post_claim")])
def test_provider_phase_uses_matching_real_attempt_receipts(board, binding, expected):
    import time
    tid = kb.create_task(board, title="provider", assignee="alice")
    kb.claim_task(board, tid)
    task = kb.get_task(board, tid)
    run = task.current_run_id
    with kb.write_txn(board):
        if binding != "missing":
            kb._append_event(board, tid, "worker_bound", {
                "pid": 222, "started_at": "birth|2",
                "claim_lock": "foreign" if binding == "wrong_lock" else task.claim_lock,
            }, run_id=run)
        if binding == "prior_attempt":
            kb._append_event(board, tid, "rate_limited", {"retry_status": "ready"}, run_id=run + 1)
        else:
            kb._append_event(board, tid, "rate_limited", {"retry_status": "ready"}, run_id=run)
    now = time.time()
    evidence.record(board, dispatch.DispatchResult(rate_limited=[tid]), board="default", machine="m", now=now)
    out = evidence.read(board, kb.get_task(board, tid), now=now)
    assert out["reasons"][0]["phase"] == expected
    if expected == "post_start":
        assert out["reasons"][0]["run_id"] == run
        assert out["reasons"][0]["process_ref"] is not None


@pytest.mark.parametrize("column,value", [("assignee", "bob"), ("runs_on", '["linux"]')])
def test_routing_identity_change_invalidates_evidence(board, column, value):
    if column == "runs_on":
        from hermes_cli import kanban_db_sync as sync
        sync._add_column_if_missing(board, "tasks", "runs_on", "runs_on TEXT")
    tid = kb.create_task(board, title="one", assignee="alice")
    evidence.record(board, dispatch.DispatchResult(skipped_ineligible=[(tid, "labels_mismatch")]),
                    board="default", machine="m", now=1000)
    with kb.write_txn(board):
        board.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, tid))
    out = evidence.read(board, kb.get_task(board, tid), now=1001)
    assert out["freshness"] == "unknown" and out["unknown_reason"] == "lifecycle_changed"


def test_truncated_task_coverage_is_explicit(board, monkeypatch):
    monkeypatch.setattr(evidence, "TASK_LIMIT", 1)
    ids = [kb.create_task(board, title="one", assignee="alice") for _ in range(2)]
    evidence.record(board, dispatch.DispatchResult(), board="default", machine="m", now=1000)
    outs = [evidence.read(board, kb.get_task(board, tid), now=1001) for tid in ids]
    assert all(o["coverage_truncated"] for o in outs)
    missing = next(o for o in outs if o["freshness"] == "unknown")
    assert missing["unknown_reason"] == "task_coverage_truncated"


def test_snapshot_assembly_does_not_hold_writer_lock(board, monkeypatch):
    import sqlite3
    tid = kb.create_task(board, title="one", assignee="alice")
    path = board.execute("PRAGMA database_list").fetchone()[2]
    with kb.write_txn(board):
        board.execute("CREATE TABLE fixture_marker (n INTEGER)")
        board.execute("INSERT INTO fixture_marker VALUES(0)")
    original = evidence._lifecycle
    writes = []
    def observe(conn, task_id):
        with closing(sqlite3.connect(path, timeout=.5)) as other, other:
            other.execute("UPDATE fixture_marker SET n=n+1")
        writes.append(task_id)
        return original(conn, task_id)
    monkeypatch.setattr(evidence, "_lifecycle", observe)
    assert evidence.record(board, dispatch.DispatchResult(), board="default", machine="m", now=1000)
    assert writes == [tid]
    assert board.execute("SELECT n FROM fixture_marker").fetchone()[0] == 1


def test_capacity_schema_covers_current_stale_missing_and_changed(board):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).parents[2] / "schemas" /
                         "kanban-capacity-evidence-v1.schema.json").read_text())
    validator = Draft202012Validator(schema)
    tid = kb.create_task(board, title="schema", assignee="alice")
    task = kb.get_task(board, tid)
    validator.validate(evidence.read(board, task, now=1000))
    evidence.record(board, dispatch.DispatchResult(memory_pressure="critical"), board="default", machine="m", now=1000)
    for now in (1001, 1180):
        validator.validate(evidence.read(board, task, now=now))
    kb.claim_task(board, tid)
    validator.validate(evidence.read(board, kb.get_task(board, tid), now=1001))


def test_read_never_invents_generation_from_an_event_or_poll(board):
    tid = kb.create_task(board, title="unclaimed", assignee="alice")
    evidence.record(board, dispatch.DispatchResult(), board="default", machine="m", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), board="default", now=1001)
    assert out["freshness"] == "current"
    observation = out["execution_observation"]
    assert "generation" not in observation["identity"]
    assert "run_id" not in observation["identity"]
    assert observation["state"] == "unknown"


def test_recorded_source_validity_is_the_sidebar_bound_not_longer(board):
    tid = kb.create_task(board, title="one", assignee="alice")
    evidence.record(board, dispatch.DispatchResult(), board="default", machine="m", now=1000)
    out = evidence.read(board, kb.get_task(board, tid), board="default", now=1001)
    assert out["valid_until_ms"] - out["observed_at_ms"] == 180_000
    assert out["execution_observation"]["valid_until"] == 1180


def test_describe_without_observed_restrictions_never_asserts_availability(board):
    tid = kb.create_task(board, title="one", assignee="alice")
    evidence.record(board, dispatch.DispatchResult(), board="default", machine="m", now=1000)
    text = evidence.describe(evidence.read(board, kb.get_task(board, tid), board="default", now=1001))
    assert text == "current: restriction coverage incomplete; admissible capacity unknown"
    assert "no restriction" not in text


def test_describe_names_observing_machine_for_machine_scoped_restrictions(board):
    """A card pinned to worker-1 seen by worker-4's dispatcher: worker-4's limits are named as worker-4's."""
    from hermes_cli import kanban_db_sync as sync
    sync._add_column_if_missing(board, "tasks", "runs_on", "runs_on TEXT")
    tid = kb.create_task(board, title="pinned", assignee="alice")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET runs_on=? WHERE id=?", ('["machine:worker-1"]', tid))
    result = dispatch.DispatchResult(at_capacity="max_in_progress=10/10", memory_pressure="critical",
                                     skipped_ineligible=[(tid, "labels_mismatch")])
    evidence.record(board, result, board="default", machine="worker-4", now=1000, update_drain=True)
    text = evidence.describe(evidence.read(board, kb.get_task(board, tid), board="default", now=1001))
    assert "at_capacity (max_in_progress=10/10) on worker-4" in text
    assert "capacity.update_drain (suppress_spawn; pre_claim) on worker-4" in text
    assert "capacity.memory_pressure (critical; pre_claim) on worker-4" in text
    # Card-scoped reasons are the card's own and stay unattributed.
    assert "capacity.placement (labels_mismatch; pre_claim)" in text
    assert "labels_mismatch; pre_claim) on" not in text
    assert text.endswith("admissible capacity unknown")


@pytest.mark.parametrize("machine", [None, ""])
def test_describe_without_machine_keeps_plain_reason(machine):
    value = {"freshness": "current", "machine": machine, "reasons": [],
             "native_restrictions": [{"source_field": "at_capacity", "native_code": "max_spawn=3/3"}]}
    assert evidence.describe(value) == "current: at_capacity (max_spawn=3/3); admissible capacity unknown"
