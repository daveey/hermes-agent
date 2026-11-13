"""review_required refusals name the real rule (operator = outside a kanban worker run),
and kanban_complete refuses a review_required card before spending a goal-judge call."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_operator as kbo
from tools import kanban_tools as kt

OPERATOR = "an operator (outside a kanban worker run)"
IDENTITIES = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK")


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


def claimed(conn, monkeypatch, *, required=True, goal_mode=False):
    tid = kb.create_task(conn, title="Wording fixture", body="Must achieve X.", assignee="default",
                         review_required=required, goal_mode=goal_mode)
    task = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    return tid, task.current_run_id


def as_operator(monkeypatch):
    for key in (*IDENTITIES, "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(key, raising=False)


# --- item 1: refusals say "operator (outside a kanban worker run)", not "orchestrator" ---


@pytest.mark.parametrize("identity", IDENTITIES)
def test_disable_refusal_names_operator_outside_worker_run(board, monkeypatch, identity):
    tid = kb.create_task(board, title="flagged", assignee="default", review_required=True)
    monkeypatch.setenv(identity, "t_orchestrator")  # a dispatched orchestrator worker is still a worker run
    with pytest.raises(kbo.KanbanEditRefused) as refused:
        kbo.edit_operator_fields(board, tid, review_required=False)
    assert OPERATOR in str(refused.value)
    assert "orchestrator" not in str(refused.value)
    assert kb.get_task(board, tid).review_required is True


@pytest.mark.parametrize("identity", IDENTITIES)
def test_worker_approving_pending_review_without_claim_names_operator(board, monkeypatch, identity):
    tid, run = claimed(board, monkeypatch)
    assert kb.request_review(board, tid, summary="implemented", expected_run_id=run)
    as_operator(monkeypatch)
    monkeypatch.setenv(identity, "t_orchestrator")
    with pytest.raises(ValueError) as refused:
        kb.complete_task(board, tid, summary="approve child", force=True)
    message = str(refused.value)
    assert OPERATOR in message and "without claiming" in message
    assert "is not its current run" not in message  # no run id was passed
    assert "kanban_request_review" not in message  # the card is already in review
    assert kb.get_task(board, tid).status == "review"


# --- comment item 3: an operator outside review is pointed at the escape hatch ---


def test_operator_cli_complete_outside_review_names_edit_escape_hatch(board, monkeypatch, capsys):
    from hermes_cli import kanban
    tid, _run = claimed(board, monkeypatch)
    as_operator(monkeypatch)
    rc = kanban._cmd_complete(argparse.Namespace(
        task_ids=[tid], result=None, summary="operator done", metadata=None, force=True))
    out = capsys.readouterr()
    assert rc != 0
    assert f"hermes kanban edit {tid} --review-required off" in out.err + out.out
    assert kb.get_task(board, tid).status == "running"


def test_worker_refusal_does_not_offer_operator_escape_hatch(board, monkeypatch):
    tid, run = claimed(board, monkeypatch)
    with pytest.raises(ValueError) as refused:
        kb.complete_task(board, tid, summary="skip", expected_run_id=run)
    assert "kanban_request_review" in str(refused.value)
    assert "--review-required off" not in str(refused.value)
    assert "A kanban worker run cannot complete" in str(refused.value)  # orchestrators too, not only implementers
    assert "implementer" not in str(refused.value)


# --- PR #1879 review nits: an in-review card is never told it "is not in review" ---


@pytest.mark.parametrize("identity", (None, *IDENTITIES))
def test_in_review_card_with_stale_run_id_is_named_pending_review(board, monkeypatch, identity):
    tid, run = claimed(board, monkeypatch)
    assert kb.request_review(board, tid, summary="implemented", expected_run_id=run)
    as_operator(monkeypatch)
    if identity:
        monkeypatch.setenv(identity, "t_orchestrator")
    with pytest.raises(ValueError) as refused:
        kb.complete_task(board, tid, summary="approve", expected_run_id=run, force=True)
    message = str(refused.value)
    assert "pending review" in message and f"Run {run} is not its current run" in message
    assert "not in review" not in message
    assert "kanban_request_review" not in message  # the card is already in review
    assert kb.get_task(board, tid).status == "review"  # still refused: no permission widened


# --- PR #1926 review nit: the operator, who may approve, is told how ---


@pytest.mark.parametrize("identity", (None, *IDENTITIES))
def test_operator_stale_run_refusal_names_the_remedy(board, monkeypatch, identity):
    tid, run = claimed(board, monkeypatch)
    assert kb.request_review(board, tid, summary="implemented", expected_run_id=run)
    as_operator(monkeypatch)
    if identity:
        monkeypatch.setenv(identity, "t_orchestrator")
    with pytest.raises(ValueError) as refused:
        kb.complete_task(board, tid, summary="approve", expected_run_id=run, force=True)
    remedy = "without expected_run_id" in str(refused.value)
    assert remedy is (identity is None)  # worker lanes keep their text: they may not approve unclaimed
    assert kb.get_task(board, tid).status == "review"  # the refusal itself stands
    if identity is None:  # and the remedy it names works
        assert kb.complete_task(board, tid, summary="approve", force=True)
        assert kb.get_task(board, tid).status == "done"


def _dashboard(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    plugin = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_kanban_wording_test", plugin)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def test_dashboard_done_outside_review_names_edit_escape_hatch(board, monkeypatch, tmp_path):
    tid, _run = claimed(board, monkeypatch)
    as_operator(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    r = _dashboard(tmp_path).patch(f"/api/plugins/kanban/tasks/{tid}",
                                   json={"status": "done", "summary": "operator done"})
    assert r.status_code == 400, r.text
    assert f"hermes kanban edit {tid} --review-required off" in r.json()["detail"]
    assert kb.get_task(board, tid).status == "running"


# --- item 2: review_required refuses before the goal judge runs ---


@pytest.mark.parametrize("required", [True, False])
def test_review_required_refuses_before_goal_judge(board, monkeypatch, required):
    tid, _run = claimed(board, monkeypatch, required=required, goal_mode=True)
    calls = []

    def judge(goal, last_response, **kw):
        calls.append(goal)
        return "done", "looks done", False, None, False

    monkeypatch.setattr(kt, "judge_goal", judge)
    monkeypatch.setattr(kt, "_goal_judge_available", lambda: True)
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "implemented"}))
    if required:
        assert out.get("rejection") == "review_required", out
        assert calls == []  # the deterministic refusal spends no judge call
        assert kb.get_task(board, tid).status == "running"
    else:  # control: without the requirement the judge still gates completion
        assert out.get("ok"), out
        assert len(calls) == 1
