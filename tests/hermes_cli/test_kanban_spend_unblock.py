"""Spend decisions cannot be acknowledged by an ordinary unblock. [daveey]"""
import argparse
import json

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_release_unread_comments as helpers
from tests.hermes_cli import test_kanban_supervisor_resume as supervisor

board = helpers.board
supervisor_board = supervisor.board


def stop(conn, tid):
    with kb.write_txn(conn):
        kb._append_event(conn, tid, decision.EVENT, {"reason": "15 runs spent"})


@pytest.mark.parametrize("entry", ["cli", "tool", "db", "supervisor"])
def test_unblock_refuses_pending_spend_decision(board, entry, capsys):
    tid = board
    with kbc.connect_closing() as conn:
        stop(conn, tid)
    before = helpers.snapshot(tid)
    if entry == "cli":
        args = argparse.Namespace(task_id=tid, task_ids=[tid], ids=None, reason=None, seen_comment_id=None)
        assert cli._cmd_unblock(args) != 0
        output = capsys.readouterr()
        text = output.out + output.err
        assert "released:" not in text
    elif entry == "tool":
        from tools import kanban_tools as kt
        result = json.loads(kt._handle_unblock({"task_id": tid}))
        assert not result.get("ok")
        text = json.dumps(result)
    elif entry == "supervisor":
        from hermes_cli import kanban_resume_exec as resume
        with kbc.connect_closing() as conn:
            text = resume.refusal(conn, "requester", tid)
    else:
        with kbc.connect_closing() as conn:
            assert not kb.unblock_task(conn, tid, comment=("operator", "try again"))
            text = kb.unblock_refusal(conn, tid)
    assert f"hermes kanban promote {tid}" in text
    assert helpers.snapshot(tid) == before


def test_hold_status_flip_is_audited_once(board, monkeypatch):
    monkeypatch.setattr(decision, "safe_ensure_producer", lambda *a: None)
    with kbc.connect_closing() as conn:
        stop(conn, board)
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (board,))
        conn.commit()
        assert budget.hold(conn, board) == "triage"
        assert kb.get_task(conn, board).status == "triage"
        events = [e for e in kb.list_events(conn, board) if e.kind == "spend_budget_held"]
        assert len(events) == 1
        assert events[0].payload["reason"]
        assert decision.pending(conn, board)
        budget.hold(conn, board)
        assert len([e for e in kb.list_events(conn, board) if e.kind == "spend_budget_held"]) == 1


def test_promotion_releases_spend_decision(board):
    with kbc.connect_closing() as conn:
        stop(conn, board)
        ok, error = kb.promote_task(conn, board, actor="operator", reason="split scope")
        assert ok, error
        assert not decision.pending(conn, board)
        assert kb.unblock_refusal(conn, board) is None


def test_supervisor_drain_rechecks_spend_stop(supervisor_board, monkeypatch):
    b = supervisor_board
    out = supervisor._tool(supervisor._resume(b["target"]))
    assert out["ok"] and out["status"] == "pending"
    supervisor._as_executor(monkeypatch)
    with kbc.connect_closing() as conn:
        stop(conn, b["target"])
        assert supervisor._tick(conn) == [(out["request_id"], "rejected")]
        req = supervisor.ksr.get_request(conn, out["request_id"])
        assert f"hermes kanban promote {b['target']}" in req["error"]
        assert kb.get_task(conn, b["target"]).status == "triage"
        assert not supervisor._resume_events(conn, b["target"])
