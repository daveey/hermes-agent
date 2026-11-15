"""Authenticated completion stages selected bytes before outer receipt I/O."""
import contextlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_preflight as preflight
from hermes_cli import kanban_source_registration as registration
from tests.hermes_cli import test_kanban_source_registration as helpers
from tools import kanban_tools as kt

setup = helpers.setup


@pytest.mark.parametrize("surface", ["complete", "request_review"])
@pytest.mark.parametrize("identity", ["owner", "foreign", "unbound", "stale"])
def test_completion_stages_only_bound_owner_before_receipt(setup, tmp_path, monkeypatch, identity, surface):
    conn, _, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    src = tmp_path / "report.txt"
    src.write_bytes(b"report")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid if identity != "foreign" else "another-task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.run_id if identity != "stale" else run.run_id + 1))
    if identity == "unbound":
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    monkeypatch.setattr(kt, "_goal_gate", lambda *a, **k: None)

    @contextlib.contextmanager
    def board_context(*a, **k):
        yield kb, conn

    monkeypatch.setattr(kt, "_board", board_context)
    observed = []

    def compute(c, task_id, attached, **kw):
        entries = artifacts.selected(c, task_id, attached.run_uid)
        assert entries and len(entries) == 1
        observed.append("receipt")
        return {"run_uid": attached.run_uid, "state": "retained", "manifest_revision": 0, "commit": None}

    @preflight.source_receipt_preflight
    def complete(c, task_id, **kw):
        assert observed == ["receipt"]
        stored = Path(kw["metadata"]["artifacts"][0])
        assert stored != src and stored.read_bytes() == b"report"
        observed.append("ending")
        return (False, "controlled stop") if surface == "request_review" else False

    monkeypatch.setattr(preflight, "compute_receipt", compute)
    monkeypatch.setattr(kb, "request_review" if surface == "request_review" else "complete_task", complete)
    handler = getattr(kt, f"_handle_{surface}")
    out = json.loads(handler({"task_id": tid, "summary": "report", "artifacts": [str(src)]}))
    if identity == "owner":
        assert observed == ["receipt", "ending"], out
        assert len(kb.list_attachments(conn, tid)) == 1
    else:
        assert observed == [], out
        assert artifacts.selected(conn, tid, run.run_uid) is None
        assert kb.list_attachments(conn, tid) == []


@pytest.mark.parametrize("surface", ["complete", "request_review"])
@pytest.mark.parametrize("selection", ["absent", "empty"])
def test_ending_distinguishes_absent_from_empty_selection(setup, tmp_path, monkeypatch, surface, selection):
    conn, _, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    src = tmp_path / "report.txt"
    src.write_bytes(b"report")
    artifacts.stage_for_run(conn, tid, [str(src)], board=kb.get_current_board(), expected_run_id=run.run_id)
    original = artifacts.selected(conn, tid, run.run_uid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run.run_id))
    monkeypatch.setattr(kt, "_goal_gate", lambda *a, **k: None)

    @contextlib.contextmanager
    def board_context(*a, **k):
        yield kb, conn

    monkeypatch.setattr(kt, "_board", board_context)
    observed = []

    def compute(c, task_id, attached, **kw):
        observed.append(artifacts.selected(c, task_id, attached.run_uid))
        return {"run_uid": attached.run_uid, "state": "retained", "manifest_revision": 0, "commit": None}

    @preflight.source_receipt_preflight
    def ending(c, task_id, **kw):
        return (False, "controlled stop") if surface == "request_review" else False

    monkeypatch.setattr(preflight, "compute_receipt", compute)
    monkeypatch.setattr(kb, "request_review" if surface == "request_review" else "complete_task", ending)
    args = {"task_id": tid, "summary": "report"}
    if selection == "empty":
        args["artifacts"] = []
    getattr(kt, f"_handle_{surface}")(args)
    assert observed == [original if selection == "absent" else []]
    assert len(kb.list_attachments(conn, tid)) == 1  # deselection does not destroy custody
