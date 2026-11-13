"""Structured review requirements survive every completion entry point."""
import argparse
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tools import kanban_tools as kt


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID", "HERMES_KANBAN_WORKER_TASK"):
        monkeypatch.delenv(key, raising=False)
    kb._INITIALIZED_PATHS.clear()
    with kbc.connect_closing() as conn:
        yield conn


def claimed(conn, monkeypatch, required=True):
    tid = kb.create_task(conn, title="Review gate fixture", assignee="default", review_required=required)
    task = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    return tid, task.current_run_id


@pytest.mark.parametrize("surface", ["tool", "cli"])
@pytest.mark.parametrize("required", [True, False])
def test_completion_gate(board, monkeypatch, capsys, surface, required):
    tid, _run = claimed(board, monkeypatch, required)
    if surface == "tool":
        out = json.loads(kt._handle_complete({"task_id": tid, "summary": "implemented"}))
        assert bool(out.get("error")) == required
        if required:
            assert out["rejection"] == "review_required"
            assert "kanban_request_review" in out["error"]
    else:
        from hermes_cli import kanban
        rc = kanban._cmd_complete(argparse.Namespace(
            task_ids=[tid], result=None, summary="implemented", metadata=None, force=True))
        assert (rc != 0) == required
        if required:
            output = capsys.readouterr()
            assert "kanban_request_review" in output.err + output.out
    assert kb.get_task(board, tid).status == ("running" if required else "done")
    if required:
        assert kb.latest_run(board, tid).ended_at is None


@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_reviewer_completion_allowed(board, monkeypatch, surface):
    tid, run = claimed(board, monkeypatch)
    assert kb.request_review(board, tid, summary="implemented", expected_run_id=run)
    reviewer = kb.claim_review_task(board, tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(reviewer.current_run_id))
    if surface == "tool":
        out = json.loads(kt._handle_complete({"task_id": tid, "summary": "review approved"}))
        assert out.get("ok"), out
    else:
        from hermes_cli import kanban
        assert kanban._cmd_complete(argparse.Namespace(
            task_ids=[tid], result=None, summary="review approved", metadata=None, force=False)) == 0
    assert kb.get_task(board, tid).status == "done"


def test_operator_can_approve_review_but_implementer_cannot(board, monkeypatch):
    tid, run = claimed(board, monkeypatch)
    assert kb.request_review(board, tid, summary="implemented", expected_run_id=run)
    with pytest.raises(ValueError, match="kanban_request_review"):
        kb.complete_task(board, tid, summary="skip", expected_run_id=run)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    assert kb.complete_task(board, tid, summary="operator approved")


@pytest.mark.parametrize("identity", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK"])
def test_pending_review_worker_cli_cannot_approve(board, monkeypatch, identity):
    from hermes_cli import kanban
    tid, run = claimed(board, monkeypatch)
    assert kb.request_review(board, tid, summary="implemented", expected_run_id=run)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    monkeypatch.setenv(identity, tid)
    assert kanban._cmd_complete(argparse.Namespace(
        task_ids=[tid], result=None, summary="self approval", metadata=None, force=True)) != 0
    assert kb.get_task(board, tid).status == "review"


def test_rework_requires_another_review(board, monkeypatch):
    tid, run = claimed(board, monkeypatch)
    assert kb.request_review(board, tid, summary="implemented", expected_run_id=run)
    reviewer = kb.claim_review_task(board, tid)
    assert kb.request_changes(board, tid, reason="fix it", expected_run_id=reviewer.current_run_id)[0]
    implementer = kb.claim_task(board, tid)
    with pytest.raises(ValueError, match="kanban_request_review"):
        kb.complete_task(board, tid, summary="fixed", expected_run_id=implementer.current_run_id)
    assert kb.request_review(board, tid, summary="fixed", expected_run_id=implementer.current_run_id)
    reviewer = kb.claim_review_task(board, tid)
    assert kb.complete_task(board, tid, summary="approved", expected_run_id=reviewer.current_run_id)


def test_flag_rechecked_after_preflight(board, monkeypatch):
    from hermes_cli import kanban_pr_acceptance_store as store
    from hermes_cli.kanban_db_operator import edit_operator_fields
    tid, run = claimed(board, monkeypatch, required=False)
    original = store.prepare_acceptance

    def flag_during_preflight(*args, **kwargs):
        result = original(*args, **kwargs)
        edit_operator_fields(board, tid, review_required=True)
        return result

    monkeypatch.setattr(store, "prepare_acceptance", flag_during_preflight)
    with pytest.raises(ValueError, match="kanban_request_review"):
        kb.complete_task(board, tid, summary="skip", expected_run_id=run)
    assert kb.get_task(board, tid).status == "running"


def test_internal_forwarded_completion_receipt_can_settle_stub(board, monkeypatch):
    tid, run = claimed(board, monkeypatch)
    # Only the internal forwarder supplies this after verifying remote done;
    # neither the public tool schema nor CLI exposes this switch.
    assert kb.complete_task(board, tid, summary="remote card reviewed and done",
                            expected_run_id=run, placement_settled=True)
