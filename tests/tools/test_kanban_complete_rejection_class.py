"""Every kanban_complete refusal carries a stable ``rejection`` class.

Coach digests counted 331 failed kanban_complete calls in a week with no way to
tell a judge rejection from a phantom created_cards id from a stale run except by
grepping prose. The tool now stamps a machine-readable class on each tool_error.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kb


def _claim(kb, monkeypatch, **create_kw):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=create_kw.pop("title", "work"),
                             assignee="test-worker", **create_kw)
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid, run_id


def _complete(args):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_complete(args))
    assert out.get("error"), out
    assert out["rejection"] in kt.COMPLETE_REJECTION_CLASSES
    return out["rejection"]


def test_phantom_cards(board, monkeypatch):
    _claim(board, monkeypatch)
    assert _complete({"summary": "done", "created_cards": ["t_phantomdeadbeef"]}) == "phantom_cards"


def test_empty(board, monkeypatch):
    _claim(board, monkeypatch)
    assert _complete({"metadata": {"x": 1}}) == "empty"


def test_invalid_args(board, monkeypatch):
    _claim(board, monkeypatch)
    assert _complete({"summary": "done", "bogus": 1}) == "invalid_args"
    assert _complete({"summary": "done", "metadata": "not a dict"}) == "invalid_args"


def test_not_owner_other_task(board, monkeypatch):
    _claim(board, monkeypatch)
    assert _complete({"task_id": "t_someoneelse", "summary": "done"}) == "not_owner"


def test_stale_run(board, monkeypatch):
    _tid, run_id = _claim(board, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id + 1000))
    assert _complete({"summary": "done"}) == "stale_run"


def test_parents(board, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        parent = board.create_task(conn, title="parent", assignee="op")
        assert board.complete_task(conn, parent, result="shipped")
    _claim(board, monkeypatch, parents=[parent])
    with kbc.connect() as conn, board.write_txn(conn):
        conn.execute("UPDATE tasks SET status='todo', completed_at=NULL WHERE id=?", (parent,))
    assert _complete({"summary": "done"}) == "parents"


def test_goal_judge(board, monkeypatch):
    _claim(board, monkeypatch, body="Must achieve X.", goal_mode=True)
    monkeypatch.setattr("tools.kanban_tools.judge_goal",
                        lambda goal, last_response, **kw: ("continue", "no evidence", False, None, False))
    monkeypatch.setattr("tools.kanban_tools._goal_judge_available", lambda: True)
    assert _complete({"summary": "did some stuff"}) == "goal_judge"


def test_live_claim(board, monkeypatch):
    tid, _ = _claim(board, monkeypatch)
    # An env-less caller (orchestrator) on a card a live worker owns.
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn, board.write_txn(conn):
        import os
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (os.getpid(), tid))
    from tools import kanban_tools as kt

    def raise_live(*a, **k):
        raise board.LiveClaimError("a live worker owns it")
    monkeypatch.setattr(board, "complete_task", raise_live)
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "done"}))
    assert out["rejection"] == "live_claim"


def test_artifacts(board, monkeypatch):
    _claim(board, monkeypatch)

    def raise_art(*a, **k):
        raise board.ArtifactPreservationError("disk full")
    monkeypatch.setattr(board, "complete_task", raise_art)
    assert _complete({"summary": "done"}) == "artifacts"


def test_unexpected_error(board, monkeypatch):
    _claim(board, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(board, "complete_task", boom)
    assert _complete({"summary": "done"}) == "error"


def test_success_has_no_rejection(board, monkeypatch):
    from tools import kanban_tools as kt
    _claim(board, monkeypatch)
    out = json.loads(kt._handle_complete({"summary": "really done"}))
    assert out.get("ok") is True and "rejection" not in out
