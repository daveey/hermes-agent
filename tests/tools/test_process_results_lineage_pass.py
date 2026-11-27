"""Retained-result owner checks share one lineage handle per inventory pass.

Every outcome refresh walks up to MAX_RETAINED_RESULTS receipts in load_completed_results
and again in the strict owner filter. Opening a SessionDB per receipt cost about 128 opens
per refresh in the hub serve (turn-start-outcome threads at ~12k cs/s,
t_01a20286f9f3f262). One pass now opens at most one read-only handle and resolves each
parent's compression tip once; ownership results must be unchanged.
"""
import json
import time

import pytest

import hermes_state
from tools import process_registry_results as results
from tools.process_registry import ProcessRegistry


@pytest.fixture
def lineage(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "get_hermes_home", lambda: tmp_path)
    directory = tmp_path / "logs" / "process-results"
    directory.mkdir(parents=True)
    db = hermes_state.SessionDB(db_path=tmp_path / "state.db")
    # owner-root was compressed into owner-tip; unrelated-* are other chats.
    db.create_session("owner-root", "cli")
    db.end_session("owner-root", end_reason="compression")
    db.create_session("owner-tip", "cli", parent_session_id="owner-root")
    for i in range(3):
        db.create_session(f"unrelated-{i}", "cli")
    db.close()

    opens = []
    real = hermes_state.SessionDB

    class Counting(real):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("db_path", tmp_path / "state.db")
            opens.append(kwargs.get("read_only"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(hermes_state, "SessionDB", Counting)
    return directory, opens


def write(directory, pid, parent):
    data = dict(id=pid, command="true", cwd="/", task_id="task", owner_task_id="task",
                session_key="", parent_session_id=parent, started_at=time.time(), exit_code=0,
                completion_reason="exited", termination_source="", notify_on_complete=False,
                output="")
    (directory / f"{pid}.json").write_text(json.dumps(data))


def corpus(directory):
    parents = ["owner-root", "owner-tip"] + [f"unrelated-{i % 3}" for i in range(20)]
    for i, parent in enumerate(parents):
        write(directory, f"proc_{i:03d}", parent)
    return {f"proc_{i:03d}" for i, p in enumerate(parents) if p.startswith("owner")}


@pytest.mark.parametrize("strict", [True, False])
def test_load_opens_one_lineage_handle_with_same_owners(lineage, strict):
    directory, opens = lineage
    expected = corpus(directory)
    loaded = results.load_completed_results(strict=strict, owner_session_id="owner-tip")
    assert set(loaded) == expected
    # owner-root resolves through the compression chain; four foreign parents, one handle.
    assert opens == [True]


def test_strict_registry_inventory_is_one_pass(lineage, monkeypatch):
    directory, opens = lineage
    expected = corpus(directory)
    registry = ProcessRegistry()
    registry._running, registry._finished = {}, {}
    monkeypatch.setattr(registry, "_refresh_detached_session", lambda s: s)
    monkeypatch.setattr(registry, "_reconcile_local_exit", lambda s: None)
    monkeypatch.setattr(registry, "_uncollected_gone", lambda s: False)
    rows = registry.list_sessions(include_retained=True, strict=True, owner_session_id="owner-tip")
    assert {row["session_id"] for row in rows} == expected
    assert opens == [True]  # load and owner filter share the pass


def test_memo_does_not_outlive_the_pass(lineage, tmp_path):
    directory, _opens = lineage
    write(directory, "proc_late", "unrelated-0")
    assert "proc_late" not in results.load_completed_results(strict=True, owner_session_id="unrelated-0-tip")
    db = hermes_state.SessionDB(db_path=tmp_path / "state.db")
    db.end_session("unrelated-0", end_reason="compression")
    db.create_session("unrelated-0-tip", "cli", parent_session_id="unrelated-0")
    db.close()
    # A rotation between passes is seen by the next pass.
    assert "proc_late" in results.load_completed_results(strict=True, owner_session_id="unrelated-0-tip")


def test_single_call_outside_a_pass_still_closes_its_handle(lineage):
    _, opens = lineage
    assert results._owns_result("owner-tip", "owner-root") is True
    assert results._owns_result("owner-tip", "unrelated-1") is False
    assert opens == [True, True]
    assert results._lineage_pass.get() is None
