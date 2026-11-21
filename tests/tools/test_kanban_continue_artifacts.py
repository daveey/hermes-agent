"""Continue's registered tool carries portable selections, not chat deliverables."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_continuation as continuation
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_registration as registration
from tests.hermes_cli import test_kanban_source_registration as helpers
from tests.tools.test_kanban_source_heartbeat import _bind
from tools import kanban_tools as kt

setup = helpers.setup


def call(args):
    entry = kt.registry.get_entry("kanban_continue")
    assert entry is not None
    return json.loads(entry.handler(args))


def test_registered_schema_exposes_continue_selection_only():
    props = kt.registry.get_schema("kanban_continue")["parameters"]["properties"]
    assert props["artifacts"]["type"] == "array"
    assert props["artifacts"]["items"] == {"type": "string"}
    for name in ("move", "block", "schedule", "request_changes"):
        assert "artifacts" not in kt.registry.get_schema(f"kanban_{name}")["parameters"]["properties"]


@pytest.mark.parametrize("selection", [None, "report.txt", {}, [1]])
def test_invalid_selection_refuses_before_initial_attach(selection, setup, monkeypatch):
    conn, boundary, tid, source, _, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    _bind(monkeypatch, conn, tid, run_id, "owner")
    out = call({"checkpoint": "resume", "source_path": str(source), "artifacts": selection})
    assert "list of paths" in out["error"], out
    assert kb.get_task(conn, tid).status == "running"
    assert board.attached_run(conn, run_id) is None
    assert boundary.actions("source_attach", "publish_source") == []
    assert kb.list_attachments(conn, tid) == []


@pytest.mark.parametrize("identity", ["foreign", "unbound", "stale", "operator"])
def test_selection_requires_bound_worker(identity, setup, monkeypatch):
    conn, boundary, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    _bind(monkeypatch, conn, tid, run.run_id, identity)
    out = call({"task_id": tid, "checkpoint": "resume", "artifacts": []})
    assert "error" in out, out
    assert kb.get_task(conn, tid).status == "running"
    assert artifacts.selected(conn, tid, run.run_uid) is None
    assert boundary.actions("publish_source") == []


def test_selection_without_source_refuses(setup, monkeypatch):
    conn, boundary, tid, _, _, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    _bind(monkeypatch, conn, tid, run_id, "owner")
    out = call({"checkpoint": "resume", "artifacts": []})
    assert "attached" in out["error"], out
    assert kb.get_task(conn, tid).status == "running"
    assert boundary.actions("source_attach", "publish_source") == []


@pytest.mark.parametrize("selection", ["omitted", "empty", "replacement", "missing"])
def test_continuation_selection_precedes_core_and_survives_refusal(selection, setup, monkeypatch):
    conn, boundary, tid, source, scratch, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    _bind(monkeypatch, conn, tid, run.run_id, "owner")
    first, second = scratch / "first.txt", scratch / "second.txt"
    first.write_bytes(b"first snapshot")
    second.write_bytes(b"second snapshot")
    old = artifacts.stage_for_run(conn, tid, [str(first)], board=kb.get_current_board(),
                                  expected_run_id=run.run_id)
    args = {"checkpoint": "resume", "metadata": {"marker": "checkpoint only"}}
    if selection != "omitted":
        args["artifacts"] = [] if selection == "empty" else [str(second if selection == "replacement" else scratch / "missing")]
    observed = []

    def refuse(*a, **kw):
        chosen = artifacts.selected(conn, tid, run.run_uid)
        observed.append(chosen)
        assert kw["metadata"]["marker"] == "checkpoint only"
        assert "artifacts" not in kw["metadata"], "selection is not chat delivery"
        second.write_bytes(b"edited after staging")
        return {"ok": False, "error": "controlled downstream refusal"}

    core = Mock(side_effect=refuse)
    monkeypatch.setattr(continuation, "continue_task", core)
    out = call(args)
    assert "error" in out, out
    assert kb.get_task(conn, tid).status == "running"
    chosen = artifacts.selected(conn, tid, run.run_uid)
    assert boundary.actions("publish_source") == []
    if selection == "missing":
        core.assert_not_called()
        assert chosen == old
        return
    assert observed == [chosen]
    if selection == "omitted":
        assert chosen == old
    elif selection == "empty":
        assert chosen == []
    else:
        assert chosen and chosen != old
        assert Path(artifacts._row(conn, tid, chosen[0])["stored_path"]).read_bytes() == b"second snapshot"
        second.write_bytes(b"second snapshot")
    count = len(kb.list_attachments(conn, tid))
    again = call(args)
    assert "error" in again
    assert artifacts.selected(conn, tid, run.run_uid) == chosen
    assert len(kb.list_attachments(conn, tid)) == count, "same bytes replay without duplicate attachment"
