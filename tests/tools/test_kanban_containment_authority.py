"""Completion adapters preserve explicit authority decisions and their findings."""
from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_containment_lifecycle as local

board = local.board


def test_tool_refuses_then_persists_retained_episode(board, monkeypatch):
    from tools import kanban_tools as kt
    relay, _, authority, target, spec = local.setup(board)
    episode = local.contain(board, relay, target, spec)["episode"]
    monkeypatch.setenv("HERMES_KANBAN_TASK", authority)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(board, authority).current_run_id))
    args = {"summary": "Retain unsafe hold: findings unresolved"}
    out = json.loads(kt._handle_complete(args))
    assert out["code"] == "containment_authority_open"
    assert out["rejection"] in kt.COMPLETE_REJECTION_CLASSES
    assert out["episodes"][0]["episode"] == episode
    assert kb.get_task(board, authority).status == "running"
    args["metadata"] = {"containment_retained": [episode], "findings": ["Unsafe fixture remains"]}
    assert json.loads(kt._handle_complete(args))["ok"]
    assert kb.latest_run(board, authority).metadata["findings"] == ["Unsafe fixture remains"]
    assert kb.get_task(board, relay).status == "triage"


def test_cli_refuses_then_accepts_retention_even_with_force(board, capsys):
    from hermes_cli import kanban
    relay, _, authority, target, spec = local.setup(board)
    episode = local.contain(board, relay, target, spec)["episode"]
    args = SimpleNamespace(task_ids=[authority], summary="Retain: unsafe fixture", result=None,
                           metadata=None, force=True)
    assert kanban._cmd_complete(args) != 0
    assert "containment_authority_open" in capsys.readouterr().err
    assert kb.get_task(board, authority).status == "running"
    args.metadata = json.dumps({"containment_retained": [episode]})
    assert kanban._cmd_complete(args) == 0
    assert kb.get_task(board, authority).status == "done"
