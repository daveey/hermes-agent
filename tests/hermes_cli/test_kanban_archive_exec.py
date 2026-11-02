"""Supervisor ``archive`` op: cancel an unstarted card orphaned in the requester's creator tree. [daveey]

Reproduces the Ask Eve dead end: standing card S created owner B, B created QA card C, B was
archived when its PR closed, and S had no supported route to cancel C (``kanban_archive`` refused:
C was not created by S; ``kanban_block(kind='assignment')``: ``no_capable_supervisor``). Every
test runs on an isolated board under ``tmp_path``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_supervisor_requests as ksr


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    assert str(kb.kanban_db_path()).startswith(str(tmp_path))
    conn = kbc.connect()
    try:
        standing = kb.create_task(conn, title="standing", assignee="default", created_by="default")
        owner = kb.create_task(conn, title="owner", assignee="default", creator_task_id=standing)
        qa = kb.create_task(conn, title="qa", assignee="default", creator_task_id=owner)
        live_owner = kb.create_task(conn, title="live owner", assignee="default", creator_task_id=standing)
        live_qa = kb.create_task(conn, title="live owner's qa", assignee="default", creator_task_id=live_owner)
        other = kb.create_task(conn, title="another standing", assignee="default")
        other_owner = kb.create_task(conn, title="other owner", assignee="default", creator_task_id=other)
        foreign = kb.create_task(conn, title="foreign qa", assignee="default", creator_task_id=other_owner)
        # The safety edge the standing card put on QA: QA stays todo behind the never-ending standing card.
        kb.link_tasks(conn, standing, qa)
        assert kb.archive_task(conn, owner)
        assert kb.archive_task(conn, other_owner)
        kb.claim_task(conn, standing)
        run_id = kb._current_run_id(conn, standing)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", standing)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return {"standing": standing, "owner": owner, "qa": qa, "live_owner": live_owner, "live_qa": live_qa,
            "foreign": foreign, "run_id": run_id}


def _tool(args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_supervisor_request(args))


def _req(tid, **extra):
    return {"ops": [{"op": "archive", "task_id": tid, **extra}], "reason": "Eve cancelled the QA; PR closed"}


def _as_executor(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)


def _drain():
    with kbc.connect_closing() as conn:
        return ksr.drain(conn, role="test")


def _status(tid):
    with kbc.connect_closing() as conn:
        return conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def _runs(tid):
    with kbc.connect_closing() as conn:
        return conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)).fetchone()[0]


def _request(rid) -> dict:
    with kbc.connect_closing() as conn:
        req = ksr.get_request(conn, rid)
    assert req is not None
    return req


# --- the dead end this op removes ------------------------------------------------------------------

def test_worker_archive_still_refuses_the_grandchild(board):
    """No widening of kanban_archive itself: the worker route stays refused."""
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_archive({"task_id": board["qa"]}))
    assert not out.get("ok", False) and "refusing to archive" in json.dumps(out), out
    assert _status(board["qa"]) == "todo"


# --- authorized cancellation ---------------------------------------------------------------------

def test_orphaned_grandchild_is_archived_by_the_executor(board, monkeypatch):
    dry = _tool({**_req(board["qa"]), "dry_run": True})
    assert dry["ops"][0]["verdict"] == "supervisor", dry
    out = _tool(_req(board["qa"]))
    assert out["ok"] and out["status"] == "pending", out
    assert _status(board["qa"]) == "todo"  # the worker call itself changes nothing
    _as_executor(monkeypatch)
    assert _drain()["processed"] == [(out["request_id"], "done")]
    req = _request(out["request_id"])
    (res,) = req["result"]
    assert res["outcome"] == "applied" and res["readback"] == "archived", res
    assert _status(board["qa"]) == "archived"
    assert _runs(board["qa"]) == 0
    with kbc.connect_closing() as conn:
        ev = [e.payload for e in kb.list_events(conn, board["qa"]) if e.kind == "archived"]
    assert ev and ev[-1] and ev[-1]["supervisor_request_id"] == out["request_id"], ev
    assert ev[-1]["requested_by"] == board["standing"] and ev[-1]["source_status"] == "todo", ev
    assert _status(board["standing"]) == "running"  # the receipt never completes the requester


def test_redelivered_request_reports_already_applied(board, monkeypatch):
    out = _tool(_req(board["qa"]))
    _as_executor(monkeypatch)
    _drain()
    req = _request(out["request_id"])
    from hermes_cli import kanban_archive_exec as ax
    seen: list[dict] = []
    monkeypatch.setattr(ksr, "_finish", lambda conn, req, executor, status, **kw: seen.append({"status": status, **kw}))
    with kbc.connect_closing() as conn:
        assert ax.applied_by(conn, board["qa"], out["request_id"])
        assert ax.execute(conn, req, "test:redeliver") == "done"
    (res,) = seen[-1]["results"]
    assert res["outcome"] == "already_applied" and res["readback"] == "archived", res
    with kbc.connect_closing() as conn:
        assert sum(e.kind == "archived" for e in kb.list_events(conn, board["qa"])) == 1


# --- unauthorized refusal ------------------------------------------------------------------------

def test_card_created_directly_is_pointed_at_kanban_archive(board):
    out = _tool(_req(board["live_owner"]))
    assert out.get("code") == "refused" and "kanban_archive" in out["error"], out
    assert _status(board["live_owner"]) != "archived"


def test_grandchild_of_a_live_creator_is_refused(board):
    out = _tool(_req(board["live_qa"]))
    assert out.get("code") == "refused" and "still open" in out["error"], out
    assert _status(board["live_qa"]) != "archived"


def test_card_outside_the_creator_tree_is_refused(board):
    out = _tool(_req(board["foreign"]))
    assert out.get("code") == "refused" and "creator tree" in out["error"], out
    assert _status(board["foreign"]) != "archived"


def test_self_and_unknown_are_refused(board):
    out = _tool(_req(board["standing"]))
    assert out.get("code") == "refused" and "self_scope" in out["error"], out
    out = _tool(_req("t_nope"))
    assert out.get("code") == "refused" and "unknown task" in out["error"], out


def test_claimed_grandchild_is_refused_and_never_stopped(board, monkeypatch):
    with kbc.connect_closing() as conn:
        kb.unlink_tasks(conn, board["standing"], board["qa"])
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, board["qa"])
    out = _tool(_req(board["qa"]))
    assert out.get("code") == "refused" and "claimed or running" in out["error"], out
    assert _status(board["qa"]) == "running"


def test_reopened_creator_is_refused_at_execution(board, monkeypatch):
    """An orphan whose creator reopens between submit and drain is no longer in scope."""
    assert _status(board["owner"]) == "archived"
    out = _tool(_req(board["qa"]))
    assert out["ok"] and out["status"] == "pending", out
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (board["owner"],))
        conn.commit()
    assert _status(board["owner"]) == "ready"
    _as_executor(monkeypatch)
    assert _drain()["processed"] == [(out["request_id"], "rejected")]
    req = _request(out["request_id"])
    assert req["status"] == "rejected", req
    (res,) = req["result"]
    assert res["outcome"] == "not_applied" and "still open" in res["detail"], res
    assert _status(board["qa"]) == "todo"


def test_claimed_target_is_rechecked_at_execution(board, monkeypatch):
    """A target claimed between submit and drain is not archived."""
    out = _tool(_req(board["qa"]))
    with kbc.connect_closing() as conn:
        kb.unlink_tasks(conn, board["standing"], board["qa"])
        kb.recompute_ready(conn)
        assert kb.claim_task(conn, board["qa"])
    _as_executor(monkeypatch)
    assert _drain()["processed"] == [(out["request_id"], "rejected")]
    (res,) = _request(out["request_id"])["result"]
    assert res["outcome"] == "not_applied" and "claimed or running" in res["detail"], res
    assert _status(board["qa"]) == "running"


def test_a_worker_cannot_drain_the_request(board):
    _tool(_req(board["qa"]))
    with kbc.connect_closing() as conn:
        with pytest.raises(ksr.SupervisorRequestError):
            ksr.drain(conn, role="test")
    assert _status(board["qa"]) == "todo"


def test_archive_ops_do_not_mix_with_other_families(board):
    out = _tool({"ops": [{"op": "archive", "task_id": board["qa"]},
                         {"op": "unlink", "parent_id": board["standing"], "child_id": board["qa"]}]})
    assert out.get("code") == "invalid_ops", out
    out = _tool(_req(board["qa"], parent_id=board["standing"]))
    assert out.get("code") == "invalid_ops", out
