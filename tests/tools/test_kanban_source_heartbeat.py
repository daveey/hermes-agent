"""Mid-run scratch source registration rides the authenticated heartbeat tool.

The worker terminal is fenced from Kanban CLI writes on purpose; the bound
kanban_heartbeat caller (task scope + dispatcher run id) is the supported path.
"""
import contextlib
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_launch as launch
from hermes_cli import kanban_source_registration as registration
from tests.hermes_cli import test_kanban_source_registration as helpers
from tools import kanban_tools as kt

setup = helpers.setup


def _bind(monkeypatch, conn, tid, run_id, identity):
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid if identity != "foreign" else "another-task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id if identity != "stale" else run_id + 1))
    if identity in ("unbound", "operator"):
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    if identity == "operator":
        monkeypatch.delenv("HERMES_KANBAN_TASK")

    @contextlib.contextmanager
    def board_context(*a, **k):
        yield kb, conn

    monkeypatch.setattr(kt, "_board", board_context)


@pytest.mark.parametrize("identity", ["owner", "foreign", "unbound", "stale", "operator"])
def test_heartbeat_registers_initial_source_only_for_bound_owner(setup, tmp_path, monkeypatch, identity):
    conn, _, tid, source, scratch, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    report = tmp_path / "report.txt"
    report.write_bytes(b"report")
    _bind(monkeypatch, conn, tid, run_id, identity)
    out = json.loads(kt._handle_heartbeat(
        {"task_id": tid, "note": "adopting source", "source_path": str(source), "artifacts": [str(report)]}))
    run = board.attached_run(conn, run_id)
    if identity == "owner":
        assert out.get("ok") is True, out
        assert out["source"]["mode"] == "initial" and out["source"]["state"] == "attached"
        assert out["source"]["artifacts"] == 1
        assert run.mode == "initial" and run.state == "attached" and run.path == str(source)
        assert len(artifacts.selected(conn, tid, run.run_uid)) == 1
        assert kb.get_task(conn, tid).workspace_path == str(scratch)
    else:
        assert "error" in out and "unknown parameter" not in out["error"], out
        assert run is None
        assert kb.list_attachments(conn, tid) == []


def test_heartbeat_without_source_fields_is_unchanged(setup, monkeypatch):
    conn, _, tid, _, _, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    _bind(monkeypatch, conn, tid, run_id, "owner")
    out = json.loads(kt._handle_heartbeat({"task_id": tid, "note": "alive"}))
    assert out == {"ok": True, "task_id": tid}
    assert board.attached_run(conn, run_id) is None


def test_heartbeat_artifacts_need_an_attached_run(setup, tmp_path, monkeypatch):
    conn, _, tid, _, _, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    report = tmp_path / "report.txt"
    report.write_bytes(b"report")
    _bind(monkeypatch, conn, tid, run_id, "owner")
    out = json.loads(kt._handle_heartbeat({"task_id": tid, "artifacts": [str(report)]}))
    assert "error" in out and "attached" in out["error"], out
    assert kb.list_attachments(conn, tid) == []


def test_heartbeat_reselects_artifacts_after_registration(setup, tmp_path, monkeypatch):
    conn, _, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    artifacts.stage_for_run(conn, tid, [str(first)], board=kb.get_current_board(), expected_run_id=run.run_id)
    _bind(monkeypatch, conn, tid, run.run_id, "owner")
    out = json.loads(kt._handle_heartbeat({"task_id": tid, "artifacts": [str(second)]}))
    assert out["source"]["artifacts"] == 1, out
    (entry,) = artifacts.selected(conn, tid, run.run_uid)
    assert artifacts._row(conn, tid, entry)["filename"] == "b.txt"
    out = json.loads(kt._handle_heartbeat({"task_id": tid, "artifacts": []}))
    assert out["source"]["artifacts"] == 0 and artifacts.selected(conn, tid, run.run_uid) == []


def test_register_initial_refuses_a_caller_run_that_is_not_current(setup):
    conn, _, tid, source, _, _, _ = setup
    run_id = kb.get_task(conn, tid).current_run_id
    with pytest.raises(launch.AttachRefused, match="caller run"):
        registration.register_initial(conn, tid, str(source), expected_run_id=run_id + 1)
    assert board.attached_run(conn, run_id) is None
