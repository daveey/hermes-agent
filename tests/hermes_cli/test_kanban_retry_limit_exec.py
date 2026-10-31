"""Supervisor ``retry_limit`` op: a worker sets the retry breaker on a card it created. [daveey]

Reproduces the runners-board gap: card t_01a192e86a794e59 needed ``max_retries=5`` but a worker
had no supported route to set it (``kanban_create`` had no such field, and the worker shell is
fenced from ``hermes kanban edit``). Every test runs on an isolated board under ``tmp_path``.
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
        requester = kb.create_task(conn, title="shepherd", assignee="default", created_by="default")
        child = kb.create_task(conn, title="flaky runner job", assignee="default", created_by="default",
                               creator_task_id=requester)
        other = kb.create_task(conn, title="another shepherd", assignee="default")
        foreign = kb.create_task(conn, title="someone else's card", assignee="default", creator_task_id=other)
        grandchild = kb.create_task(conn, title="grandchild", assignee="default", creator_task_id=child)
        kb.claim_task(conn, requester)
        run_id = kb._current_run_id(conn, requester)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", requester)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return {"requester": requester, "child": child, "foreign": foreign, "grandchild": grandchild,
            "run_id": run_id}


def _tool(args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_supervisor_request(args))


def _req(tid, n=5, **extra):
    return {"ops": [{"op": "retry_limit", "task_id": tid, "max_retries": n, **extra}],
            "reason": "runner card needs a wider breaker"}


def _as_executor(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)


def _drain():
    with kbc.connect_closing() as conn:
        return ksr.drain(conn, role="test")


def _max_retries(tid):
    with kbc.connect_closing() as conn:
        return conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def _request(rid) -> dict:
    with kbc.connect_closing() as conn:
        req = ksr.get_request(conn, rid)
    assert req is not None
    return req


# --- authorized apply ----------------------------------------------------------------------------

def test_in_scope_retry_limit_applies_and_reads_back(board, monkeypatch):
    dry = _tool({**_req(board["child"]), "dry_run": True})
    assert dry["ops"][0]["verdict"] == "supervisor", dry
    out = _tool(_req(board["child"]))
    assert out["ok"] and out["status"] == "pending", out
    assert _max_retries(board["child"]) is None
    _as_executor(monkeypatch)
    assert _drain()["processed"] == [(out["request_id"], "done")]
    req = _request(out["request_id"])
    assert req["status"] == "done", req
    (res,) = req["result"]
    assert res["outcome"] == "applied" and res["old"] is None and res["new"] == 5 and res["readback"] == 5, res
    assert _max_retries(board["child"]) == 5
    with kbc.connect_closing() as conn:
        edited = [e.payload for e in kb.list_events(conn, board["child"]) if e.kind == "edited"]
    assert edited and edited[-1]["changes"] == {"max_retries": {"old": None, "new": 5}}, edited


def test_lowering_an_existing_breaker_reports_old_value(board, monkeypatch):
    out = _tool(_req(board["child"], 3))
    _as_executor(monkeypatch)
    _drain()
    monkeypatch.setenv("HERMES_KANBAN_TASK", board["requester"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(board["run_id"]))
    out = _tool(_req(board["child"], 7))
    _as_executor(monkeypatch)
    _drain()
    (res,) = _request(out["request_id"])["result"]
    assert (res["old"], res["new"], res["readback"]) == (3, 7, 7), res


# --- scope ---------------------------------------------------------------------------------------

def test_own_card_is_refused(board):
    dry = _tool({**_req(board["requester"]), "dry_run": True})
    assert dry["ops"][0]["verdict"] == "refused" and "self_scope" in dry["ops"][0]["reason"], dry
    out = _tool(_req(board["requester"]))
    assert out.get("code") == "refused" and "self_scope" in out["error"], out
    assert _max_retries(board["requester"]) is None


@pytest.mark.parametrize("which", ["foreign", "grandchild"])
def test_card_this_task_did_not_create_is_refused(board, which):
    dry = _tool({**_req(board[which]), "dry_run": True})
    assert dry["ops"][0]["verdict"] == "refused", dry
    out = _tool(_req(board[which]))
    assert out.get("code") == "refused", out
    assert _max_retries(board[which]) is None


def test_unknown_target_is_refused(board):
    out = _tool(_req("t_nope"))
    assert out.get("code") == "refused" and "unknown task" in out["error"], out


@pytest.mark.parametrize("end", ["archived", "done"])
def test_archived_or_done_target_is_refused(board, end):
    with kbc.connect_closing() as conn:
        if end == "archived":
            assert kb.archive_task(conn, board["child"])
        else:
            assert kb.complete_task(conn, board["child"], result="x", summary="finished")
    out = _tool(_req(board["child"]))
    assert out.get("code") == "refused" and end in out["error"], out


# --- validation ----------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [0, 11, -3, True, False, "5", 2.5, None])
def test_out_of_range_or_non_int_is_invalid(board, bad):
    out = _tool(_req(board["child"], bad))
    assert out.get("code") == "invalid_ops" and "max_retries" in out["error"], out


def test_missing_task_id_or_extra_field_is_invalid(board):
    out = _tool({"ops": [{"op": "retry_limit", "max_retries": 5}]})
    assert out.get("code") == "invalid_ops", out
    out = _tool(_req(board["child"], parent_id=board["requester"]))
    assert out.get("code") == "invalid_ops", out


def test_equal_value_is_noop(board, monkeypatch):
    _tool(_req(board["child"], 4))
    _as_executor(monkeypatch)
    _drain()
    monkeypatch.setenv("HERMES_KANBAN_TASK", board["requester"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(board["run_id"]))
    dry = _tool({**_req(board["child"], 4), "dry_run": True})
    assert dry["ops"][0]["verdict"] == "noop", dry
    out = _tool(_req(board["child"], 4))
    assert out.get("code") == "no_supervisor_needed", out


@pytest.mark.parametrize("other", [
    {"op": "resume", "task_id": "t_x"},
    {"op": "unlink", "parent_id": "t_a", "child_id": "t_b"},
])
def test_mixed_families_are_refused(board, other):
    out = _tool({"ops": [{"op": "retry_limit", "task_id": board["child"], "max_retries": 5}, other]})
    assert out.get("code") == "invalid_ops" and "retry_limit" in out["error"], out


def test_schema_declares_the_op():
    from tools.kanban_tools_schemas import KANBAN_SUPERVISOR_REQUEST_SCHEMA as S
    item = S["parameters"]["properties"]["ops"]["items"]["properties"]
    assert "retry_limit" in item["op"]["enum"] and item["max_retries"]["type"] == "integer"
    assert "Op retry_limit {task_id, max_retries 1-10}" in S["description"]


# --- execution re-checks the board as it is now ----------------------------------------------------

def test_scope_lost_between_submit_and_execute_is_rejected(board, monkeypatch):
    out = _tool(_req(board["child"]))
    with kbc.connect_closing() as conn:
        assert kb.archive_task(conn, board["child"])
    _as_executor(monkeypatch)
    _drain()
    req = _request(out["request_id"])
    assert req["status"] == "rejected" and "archived" in req["error"], req
    assert req["result"][0]["outcome"] == "not_applied"
    assert _max_retries(board["child"]) is None


def test_requester_finished_before_execute_is_rejected(board, monkeypatch):
    out = _tool(_req(board["child"]))
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, board["requester"], result="x", summary="finished")
    _as_executor(monkeypatch)
    _drain()
    req = _request(out["request_id"])
    assert req["status"] == "rejected" and "requester" in req["error"], req
    assert _max_retries(board["child"]) is None


def test_redelivery_after_apply_reports_already_applied(board, monkeypatch):
    out = _tool(_req(board["child"]))
    _as_executor(monkeypatch)
    real_finish = ksr._finish
    monkeypatch.setattr(ksr, "_finish", lambda *a, **k: False)  # crash after the write, before the receipt
    with kbc.connect_closing() as conn:
        ksr.drain(conn, role="crashy")
    assert _max_retries(board["child"]) == 5
    assert _request(out["request_id"])["status"] == "accepted"
    monkeypatch.setattr(ksr, "_finish", real_finish)
    import time as _t
    with kbc.connect_closing() as conn:  # lease expired: redelivered
        ksr.drain(conn, role="test", now=int(_t.time()) + ksr.LEASE_SECONDS + 5)
    req = _request(out["request_id"])
    assert req["status"] == "done", req
    (res,) = req["result"]
    assert res["outcome"] == "already_applied" and res["readback"] == 5, res
    with kbc.connect_closing() as conn:
        edited = [e for e in kb.list_events(conn, board["child"]) if e.kind == "edited"]
    assert len(edited) == 1  # applied once


def test_worker_context_cannot_execute(board):
    _tool(_req(board["child"]))
    with kbc.connect_closing() as conn:
        with pytest.raises(ksr.SupervisorRequestError) as exc:
            ksr.drain(conn, role="test")
    assert exc.value.code == "worker_context"
    assert _max_retries(board["child"]) is None
