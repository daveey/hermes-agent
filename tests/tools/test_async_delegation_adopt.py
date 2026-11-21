"""Re-register an owned durable unit, not a new delegation or an automatic crash retry."""
import json
import os
import threading
from queue import Empty
from types import SimpleNamespace

import pytest

from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def clean_registry():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    yield
    if ad._executor is not None:
        ad._executor.shutdown(wait=True)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def seed():
    record = dict(delegation_id="deleg_adopt", goal="original", goals=["finished", "remaining"],
                  is_batch=True, task_indexes=[0, 1], context="original context", toolsets=["file"],
                  role="leaf", model="original-model", session_key="route", parent_session_id="parent",
                  origin_ui_session_id="ui", origin_session_id="origin", scope_id="scope",
                  user_id="user", user_name="name", dispatched_at=123.0,
                  slot_key="original-slot", task_transcripts={"1": "transcript"},
                  task_session_ids={"1": "child"})
    ad._persist_dispatch(record)
    ad.record_unit_child("deleg_adopt", {"task_index": 0, "status": "completed", "summary": "saved"})
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET delivery_attempts=3 WHERE delegation_id='deleg_adopt'")
    return record


def row():
    with ad._transaction() as conn:
        conn.row_factory = __import__("sqlite3").Row
        return dict(conn.execute("SELECT * FROM async_delegations WHERE delegation_id='deleg_adopt'").fetchone())


def adopt(runner=None, **kwargs):
    from tools.async_delegation_adopt import adopt_unit
    return adopt_unit("deleg_adopt", runner=runner or dict, **kwargs)


def test_adopt_keeps_identity_finished_siblings_and_single_completion():
    original = seed()
    before = row()
    entered, release = threading.Event(), threading.Event()

    def runner():
        entered.set()
        assert release.wait(10)
        return {"results": [{"task_index": 1, "status": "completed", "summary": "new"}],
                "total_duration_seconds": 2}

    try:
        assert adopt(runner) == {"status": "dispatched", "delegation_id": "deleg_adopt"}
        assert entered.wait(10)
        assert row() == before, "registration must never replace the durable row"
        record = ad._records["deleg_adopt"]
        assert record["slot_key"] == original["slot_key"]
        assert record["task_transcripts"] == original["task_transcripts"]
        assert record["task_session_ids"] == original["task_session_ids"]
        assert record["_finished_children"][0]["summary"] == "saved"
        assert record["_approval_lease"].active
        assert adopt()["status"] == "rejected"
    finally:
        release.set()
        if ad._executor:
            ad._executor.shutdown(wait=True)
    event = process_registry.completion_queue.get_nowait()
    assert event["delegation_id"] == "deleg_adopt"
    assert event["dispatched_at"] == 123.0
    for key in ("scope_id", "user_id", "user_name", "origin_ui_session_id", "origin_session_id", "parent_session_id"):
        assert event[key] == original[key]
    assert [r["summary"] for r in event["results"]] == ["saved", "new"]
    ad._finalize("deleg_adopt", {"results": []}, "error")
    with pytest.raises(Empty):
        process_registry.completion_queue.get_nowait()
    assert row()["delivery_attempts"] == 3
    assert row()["state"] == "completed"
    assert json.loads(row()["result_json"])["results"] == event["results"]
    assert adopt()["status"] == "rejected"


@pytest.mark.parametrize("changes", [
    {"owner_pid": os.getpid() + 1}, {"owner_started_at": "mismatch"}, {"owner_started_at": None},
    {"owner_started_at": "0"}, {"state": "stalled"}, {"delivery_state": "dropped"},
    {"state": "completed"}, {"state": "finalizing"}, {"delivery_state": "delivered"},
    {"completed_at": 1.0}, {"event_json": "{}"},
])
def test_unowned_or_terminal_row_is_not_reexecuted(changes):
    seed()
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET " + ",".join(f"{key}=?" for key in changes) +
                     " WHERE delegation_id='deleg_adopt'", tuple(changes.values()))
    before = row()
    assert adopt(lambda: pytest.fail("must not run"))["status"] == "rejected"
    assert row() == before
    assert "deleg_adopt" not in ad._records


def test_missing_row_and_worker_are_refused(monkeypatch):
    assert adopt()["status"] == "rejected"
    seed()
    monkeypatch.setattr(ad, "require_ledger_owner", lambda: (_ for _ in ()).throw(PermissionError("owner only")))
    with pytest.raises(PermissionError, match="owner only"):
        adopt()
    assert "deleg_adopt" not in ad._records


@pytest.mark.parametrize("failure", ["submit", "executor", "lease"])
def test_schedule_failure_preserves_row_and_releases_gates(monkeypatch, failure):
    from hermes_cli import backend_retirement
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    seed()
    before = row()
    def fail_submit(fn):
        raise RuntimeError("injected submit failure")
    monkeypatch.setattr(ad, "_get_executor", lambda size: SimpleNamespace(submit=fail_submit))
    if failure == "executor":
        monkeypatch.setattr(ad, "_get_executor", lambda size: fail_submit(None))
    if failure == "lease":
        from tools import approval_notify_lease
        monkeypatch.setattr(approval_notify_lease, "acquire", lambda key: fail_submit(None))
    assert adopt()["status"] == "rejected"
    assert row() == before
    assert "deleg_adopt" not in ad._records
    assert fence.prepare()["ok"] is True


def test_capacity_closed_session_and_retirement_remain_gates(monkeypatch):
    from hermes_cli import backend_retirement
    seed()
    before = row()
    ad._records["other"] = dict(delegation_id="other", status="running", slot_key="other")
    assert adopt(max_async_children=1)["status"] == "rejected"
    ad._records.clear()
    ad._refused_by[("parent_session_id", "parent")] = {"closed"}
    assert adopt()["status"] == "rejected"
    ad._refused_by.clear()
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    assert fence.prepare()["ok"] is True
    assert adopt()["status"] == "rejected"
    assert row() == before


def test_adopted_runner_stall_retains_results_and_worker_reservation(monkeypatch):
    from hermes_cli import backend_retirement
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    entered, release = threading.Event(), threading.Event()
    seed()
    def runner():
        entered.set()
        assert release.wait(10)
        return {"results": [{"task_index": 1, "status": "completed"}]}
    try:
        assert adopt(runner)["status"] == "dispatched"
        assert entered.wait(10)
        ad._finalize("deleg_adopt", {"error": "stalled"}, "stalled")
        assert fence.prepare() == {"ok": False, "idle": False}
    finally:
        release.set()
        if ad._executor:
            ad._executor.shutdown(wait=True)
    event = process_registry.completion_queue.get_nowait()
    assert [r["status"] for r in event["results"]] == ["completed", "stalled"]
    assert process_registry.completion_queue.empty()
    assert fence.prepare()["ok"] is True


def test_stale_read_is_refused(monkeypatch):
    from tools import async_delegation_adopt as adoption
    seed()
    old = adoption.owned_row
    reads = []
    def changed(delegation_id):
        value = old(delegation_id)
        reads.append(value)
        if len(reads) == 1:
            with ad._transaction() as conn:
                conn.execute("UPDATE async_delegations SET owner_pid=0 WHERE delegation_id=?", (delegation_id,))
        return value
    monkeypatch.setattr(adoption, "owned_row", changed)
    assert adopt(lambda: pytest.fail("stale registration ran"))["status"] == "rejected"
    assert "deleg_adopt" not in ad._records
    assert row()["owner_pid"] == 0


def test_adoption_uses_stale_monitor_and_original_pool_slot(monkeypatch):
    seed()
    ad._records["sibling"] = dict(delegation_id="sibling", status="running", slot_key="original-slot")
    monitors = []
    monkeypatch.setattr(ad, "_ensure_stale_monitor", lambda: monitors.append(True))
    release = threading.Event()
    def runner():
        assert release.wait(10)
        return {"results": [{"task_index": 1, "status": "completed"}]}
    try:
        assert adopt(runner, max_async_children=1, progress_fn=lambda: (1, False))["status"] == "dispatched"
        assert monitors == [True]
        assert ad._records["deleg_adopt"]["progress_fn"]() == (1, False)
    finally:
        release.set()
        if ad._executor:
            ad._executor.shutdown(wait=True)


def test_concurrent_adopters_publish_once():
    from tools.thread_context import propagate_context_to_thread
    seed()
    start = threading.Barrier(3)
    release = threading.Event()
    handles, calls = [], []
    def runner():
        calls.append(True)
        assert release.wait(10)
        return {"results": [{"task_index": 1, "status": "completed"}]}
    def register():
        start.wait(10)
        handles.append(adopt(runner))
    threads = [threading.Thread(target=propagate_context_to_thread(register)) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        start.wait(10)
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive()
        assert sorted(h["status"] for h in handles) == ["dispatched", "rejected"]
    finally:
        release.set()
        for thread in threads:
            thread.join(10)
        if ad._executor:
            ad._executor.shutdown(wait=True)
    assert calls == [True]
    assert process_registry.completion_queue.get_nowait()["delegation_id"] == "deleg_adopt"
    assert process_registry.completion_queue.empty()
