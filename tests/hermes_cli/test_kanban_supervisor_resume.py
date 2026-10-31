"""Supervisor ``resume`` op: a recovery card requeues the card the continuation budget parked. [daveey]

Reproduces t_01a191a2b59741ef: layers card t_01a19084a3b36956 hit the 12-run continuation limit and
went to triage; its recovery card (created by it) had no route to release it, because
``kanban_unblock`` is fenced for every worker profile. Every test runs on an isolated board under
``tmp_path``, never the live board.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_resume_exec as rx
from hermes_cli import kanban_supervisor_requests as ksr

REPO = Path(__file__).resolve().parents[2]
EXHAUSTED = "continuation_exhausted: split the work or reset the continuation budget"


def _exhaust(conn, tid):
    """Drive ``tid`` into the real continuation_exhausted triage park (limit 0 = refuse at once)."""
    assert kb.claim_task(conn, tid) is not None
    run = kb._current_run_id(conn, tid)
    out = kc.continue_task(conn, tid, checkpoint="partial", expected_run_id=run, max_continuations=0)
    assert out["reason_code"] == "continuation_exhausted", out
    assert kb.block_task(conn, tid, kind="transient", reason=EXHAUSTED, expected_run_id=run)
    assert kb.get_task(conn, tid).status == "triage"
    return run


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
    db = kb.kanban_db_path()
    assert str(db).startswith(str(tmp_path)), db
    conn = kbc.connect()
    try:
        target = kb.create_task(conn, title="layers 2-8", assignee="default", created_by="default")
        _exhaust(conn, target)
        recovery = kb.create_task(conn, title="restore dispatch", assignee="default", created_by="default",
                                  creator_task_id=target)
        foreign = kb.create_task(conn, title="someone else's long card", assignee="default")
        _exhaust(conn, foreign)
        kb.claim_task(conn, recovery)
        run_id = kb._current_run_id(conn, recovery)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", recovery)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return {"target": target, "recovery": recovery, "foreign": foreign, "run_id": run_id,
            "db": str(db), "home": str(home), "tmp": tmp_path}


def _tool(args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_supervisor_request(args))


def _resume(tid):
    return {"ops": [{"op": "resume", "task_id": tid}], "reason": "continuation budget exhausted"}


def _as_executor(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)


def _tick(conn):
    from hermes_cli import kanban_db_dispatch as kbd
    return kbd.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **k: None).supervisor_requests


def _resume_events(conn, tid):
    return [e.payload for e in kb.list_events(conn, tid) if e.kind == "unblocked"
            and (e.payload or {}).get("via") == "supervisor_resume"]


def _edges(conn):
    return {(r[0], r[1]) for r in conn.execute("SELECT parent_id, child_id FROM task_links")}


def _child_env(b):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
    env.update(HERMES_HOME=b["home"], HOME=str(b["tmp"]), HERMES_KANBAN_DB=b["db"], PYTHONPATH=str(REPO))
    return env


def _cli_drain(b):
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "supervisor-requests", "--drain", "--json"],
        env=_child_env(b), cwd=str(REPO), capture_output=True, text=True, timeout=240, check=False)


# --- authorized recovery ---------------------------------------------------------------------

def test_authorized_resume_through_the_dispatcher_tick(board, monkeypatch):
    b = board
    edges = None
    with kbc.connect() as conn:
        edges = _edges(conn)
    dry = _tool({**_resume(b["target"]), "dry_run": True})
    assert dry["ops"][0]["verdict"] == "supervisor", dry
    out = _tool(_resume(b["target"]))
    assert out["ok"] and out["status"] == "pending" and not out["duplicate"], out
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert kb.get_task(conn, b["target"]).status == "triage"  # nothing until the executor runs
        assert _tick(conn) == [(out["request_id"], "done")]
        target = kb.get_task(conn, b["target"])
        assert (target.status, target.current_run_id, target.human_request) == ("ready", None, None)
        assert _resume_events(conn, b["target"]) == [
            {"via": "supervisor_resume", "request_id": out["request_id"], "by": b["recovery"]}]
        assert any(c.author == ksr.COMMENT_AUTHOR and f"#{out['request_id']}" in c.body
                   for c in kb.list_comments(conn, b["target"]))
        req = ksr.get_request(conn, out["request_id"])
        assert req["status"] == "done"
        assert req["result"] == [{"op": "resume", "task_id": b["target"], "outcome": "applied",
                                  "status": "ready", "current_run_id": None, "unsatisfied_parents": []}]
        kinds = [e.kind for e in kb.list_events(conn, b["recovery"]) if e.kind.startswith("supervisor_request_")]
        assert kinds == ["supervisor_request_queued", "supervisor_request_accepted", "supervisor_request_done"]
        assert kb.get_task(conn, b["recovery"]).status == "running"  # the requester is not released
        assert _edges(conn) == edges  # no graph change
        # The budget streak ended with the exhausted run's block; the policy itself is unchanged.
        assert kc.consecutive_continuations(conn, b["target"]) == 0
        assert kb.claim_task(conn, b["target"]) is not None  # a new run can actually claim it
        assert kb._current_run_id(conn, b["target"]) is not None


def test_separate_cli_executor_process_applies_it(board):
    out = _tool(_resume(board["target"]))
    proc = _cli_drain(board)
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(proc.stdout[proc.stdout.index("{"):])
    assert report["drained"]["processed"] == [[out["request_id"], "done"]]
    assert str(os.getpid()) not in report["drained"]["executor"]
    with kbc.connect() as conn:
        assert kb.get_task(conn, board["target"]).status == "ready"


def test_unsatisfied_parent_keeps_it_gated_and_satisfied_parent_releases(board, monkeypatch):
    with kbc.connect() as conn:
        gate = kb.create_task(conn, title="mechanism review", assignee="default")
        kb.link_tasks(conn, gate, board["target"])
    out = _tool(_resume(board["target"]))
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert _tick(conn) == [(out["request_id"], "done")]
        assert kb.get_task(conn, board["target"]).status == "todo"
        assert ksr.get_request(conn, out["request_id"])["result"][0]["unsatisfied_parents"] == [gate]
        assert kb.claim_task(conn, board["target"]) is None
        kb.claim_task(conn, gate)
        assert kb.complete_task(conn, gate, result="ok")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, board["target"]).status == "ready"


def test_creator_chain_two_hops_is_in_scope(board, monkeypatch):
    """A fix card the recovery card spawned may resume the original too (bounded chain)."""
    with kbc.connect() as conn:
        fix = kb.create_task(conn, title="fix the route", assignee="default", creator_task_id=board["recovery"])
        kb.claim_task(conn, fix)
        run = kb._current_run_id(conn, fix)
    monkeypatch.setenv("HERMES_KANBAN_TASK", fix)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    out = _tool(_resume(board["target"]))
    assert out["ok"], out
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert _tick(conn) == [(out["request_id"], "done")]


def test_card_the_requester_created_is_in_scope_even_if_parked_after_creation(board, monkeypatch):
    """The requester owns a card it created: no created-after-park check applies to it."""
    with kbc.connect() as conn:
        own = kb.create_task(conn, title="sub-work", assignee="default", creator_task_id=board["recovery"])
        _exhaust(conn, own)
        assert rx.refusal(conn, board["recovery"], own) is None
    out = _tool(_resume(own))
    assert out["ok"], out
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert _tick(conn) == [(out["request_id"], "done")]
        assert kb.get_task(conn, own).status == "ready"


# --- refusals: scope, human gate, stale, duplicates -------------------------------------------

def test_wrong_scope_is_refused_and_nothing_moves(board):
    out = _tool(_resume(board["foreign"]))
    assert out["code"] == "refused" and "creator chain" in out["error"], out
    with kbc.connect() as conn:
        assert kb.get_task(conn, board["foreign"]).status == "triage"
        assert ksr.list_requests(conn) == []


def test_long_creator_chain_is_out_of_scope(board, monkeypatch):
    with kbc.connect() as conn:
        node = board["recovery"]
        for i in range(rx.MAX_CREATOR_HOPS):
            node = kb.create_task(conn, title=f"hop{i}", assignee="default", creator_task_id=node)
        kb.claim_task(conn, node)
        run = kb._current_run_id(conn, node)
    monkeypatch.setenv("HERMES_KANBAN_TASK", node)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    assert _tool(_resume(board["target"]))["code"] == "refused"


def _human_gate(conn, tid):
    req = {"action": "Reply A or B. Unblock = build A; archive = drop it.", "why_human": "design choice",
           "interaction": "reply", "scope": "PR 188", "evidence": "two decisions conflict"}
    assert kb.claim_task(conn, tid) is not None
    assert kb.block_task(conn, tid, kind="needs_input", reason="pick A or B", human_request=req,
                         expected_run_id=kb._current_run_id(conn, tid))
    assert kb.get_task(conn, tid).block_kind == "needs_input"


def test_genuine_human_gate_is_refused_even_with_a_resolved_comment(board):
    """score_rows shape: needs_input + human_request, and a non-human comment saying 'decided A'."""
    with kbc.connect() as conn:
        gated = kb.create_task(conn, title="score rows", assignee="default", creator_task_id=board["recovery"])
        _human_gate(conn, gated)
        kb.add_comment(conn, gated, "root-chat", "ROOT DISPOSITION: choose A. No human decision needed.")
    out = _tool(_resume(gated))
    assert out["code"] == "refused" and "human_gate" in out["error"], out
    with kbc.connect() as conn:
        task = kb.get_task(conn, gated)
        assert (task.status, task.block_kind) == ("blocked", "needs_input") and task.human_request


def test_human_gate_that_appears_after_submit_fails_closed(board, monkeypatch):
    out = _tool(_resume(board["target"]))
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET block_kind='needs_input', human_request=? WHERE id=?",
                         (json.dumps({"action": "x"}), board["target"]))
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert _tick(conn) == [(out["request_id"], "rejected")]
        assert kb.get_task(conn, board["target"]).status == "triage"
        assert _resume_events(conn, board["target"]) == []


def test_stale_requester_run_is_refused(board, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(board["run_id"] + 1000))
    assert _tool(_resume(board["target"]))["code"] == "stale_run"


def test_target_already_released_or_live_is_stale_and_fails_closed(board, monkeypatch):
    out = _tool(_resume(board["target"]))
    with kbc.connect() as conn:
        assert kb.unblock_task(conn, board["target"])  # an operator got there first
        assert kb.claim_task(conn, board["target"]) is not None  # and a worker is now live on it
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert _tick(conn) == [(out["request_id"], "rejected")]
        assert kb.get_task(conn, board["target"]).status == "running"
        assert "not parked" in ksr.get_request(conn, out["request_id"])["error"]
        assert _resume_events(conn, board["target"]) == []


def test_released_then_parked_for_another_reason_is_not_resumable(board):
    with kbc.connect() as conn:
        assert kb.unblock_task(conn, board["target"])
        assert kb.claim_task(conn, board["target"]) is not None
        assert kb.block_task(conn, board["target"], kind="transient", reason="flaky network",
                             expected_run_id=kb._current_run_id(conn, board["target"]))
    out = _tool(_resume(board["target"]))
    assert out["code"] == "refused" and "not a continuation_exhausted block" in out["error"], out


def test_completed_or_archived_requester_is_rejected_at_execution(board, monkeypatch):
    out = _tool(_resume(board["target"]))
    with kbc.connect() as conn:
        kb.archive_task(conn, board["recovery"])
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert _tick(conn) == [(out["request_id"], "rejected")]
        assert kb.get_task(conn, board["target"]).status == "triage"


def test_duplicate_request_returns_the_same_row(board):
    first = _tool(_resume(board["target"]))
    second = _tool(_resume(board["target"]))
    assert second["duplicate"] and second["request_id"] == first["request_id"]
    with kbc.connect() as conn:
        assert len(ksr.list_requests(conn)) == 1


def test_resume_cannot_be_mixed_with_edge_ops_or_extra_fields(board):
    mixed = _tool({"ops": [{"op": "resume", "task_id": board["target"]},
                           {"op": "unlink", "parent_id": board["target"], "child_id": board["recovery"]}]})
    assert mixed["code"] == "invalid_ops"
    extra = _tool({"ops": [{"op": "resume", "task_id": board["target"], "reset_budget": True}]})
    assert extra["code"] == "invalid_ops"


def _new_recovery(conn, monkeypatch, target, title="recover again"):
    rec = kb.create_task(conn, title=title, assignee="default", creator_task_id=target)
    kb.claim_task(conn, rec)
    monkeypatch.setenv("HERMES_KANBAN_TASK", rec)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(conn, rec)))
    return rec


def test_recovery_card_from_an_earlier_park_cannot_resume_the_next_one(board, monkeypatch):
    out = _tool(_resume(board["target"]))
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert _tick(conn) == [(out["request_id"], "done")]
        _exhaust(conn, board["target"])  # it ran out of budget again
    monkeypatch.setenv("HERMES_KANBAN_TASK", board["recovery"])
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(board["run_id"]))
    again = _tool(_resume(board["target"]))
    assert again["code"] == "refused" and "predates" in again["error"], again


def test_per_target_cap_bounds_repeated_resumes(board, monkeypatch):
    for n in range(rx.MAX_RESUMES):
        if n:
            with kbc.connect() as conn:
                _new_recovery(conn, monkeypatch, board["target"], f"recover {n}")
        out = _tool(_resume(board["target"]))
        assert out["ok"], out
        _as_executor(monkeypatch)
        with kbc.connect() as conn:
            assert _tick(conn) == [(out["request_id"], "done")]
            _exhaust(conn, board["target"])
    with kbc.connect() as conn:
        _new_recovery(conn, monkeypatch, board["target"], "recover once more")
    out = _tool(_resume(board["target"]))
    assert out["code"] == "refused" and "resume_budget_exhausted" in out["error"], out


def test_human_gate_landing_inside_execution_is_not_erased(board, monkeypatch):
    """Race (review finding): a needs_input ask lands after the executor's first read but before
    its write. The in-transaction precondition must see it and leave the human ask intact."""
    out = _tool(_resume(board["target"]))
    _as_executor(monkeypatch)
    real = rx.preflight_op

    def racing_preflight(conn, requester, op):
        verdict = real(conn, requester, op)
        other = kbc.connect()
        try:
            with kb.write_txn(other):
                other.execute("UPDATE tasks SET status='blocked', block_kind='needs_input', human_request=? "
                              "WHERE id=?", (json.dumps({"action": "pick A or B"}), op["task_id"]))
        finally:
            other.close()
        return verdict

    monkeypatch.setattr(rx, "preflight_op", racing_preflight)
    with kbc.connect() as conn:
        assert _tick(conn) == [(out["request_id"], "rejected")]
        task = kb.get_task(conn, board["target"])
        assert (task.status, task.block_kind) == ("blocked", "needs_input")
        assert task.human_request["action"] == "pick A or B"
        assert "human_gate" in ksr.get_request(conn, out["request_id"])["error"]
        assert _resume_events(conn, board["target"]) == []
        assert not any(c.author == ksr.COMMENT_AUTHOR for c in kb.list_comments(conn, board["target"]))


# --- crash retry -----------------------------------------------------------------------------

_CRASH_AFTER_APPLY = """
import os, sys
sys.path.insert(0, sys.argv[1])
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_supervisor_requests as ksr
ksr._finish = lambda *a, **k: os._exit(17)   # die after the unblock committed, before the receipt
with kbc.connect() as conn:
    ksr.drain(conn, role="crashy")
"""


def test_crash_after_apply_is_redelivered_and_reports_already_applied(board):
    out = _tool(_resume(board["target"]))
    crashed = subprocess.run([sys.executable, "-c", _CRASH_AFTER_APPLY, str(REPO)],
                             env=_child_env(board), capture_output=True, text=True, timeout=240, check=False)
    assert crashed.returncode == 17, crashed.stderr[-2000:]
    with kbc.connect() as conn:
        assert ksr.get_request(conn, out["request_id"])["status"] == "accepted"
        assert kb.get_task(conn, board["target"]).status == "ready"  # applied before the crash
    proc = _cli_drain(board)
    assert proc.returncode == 0, proc.stderr[-2000:]
    with kbc.connect() as conn:
        req = ksr.get_request(conn, out["request_id"])
        assert req["status"] == "done" and req["attempts"] == 2
        assert req["result"][0]["outcome"] == "already_applied"
        assert len(_resume_events(conn, board["target"])) == 1  # applied exactly once


def test_executor_refuses_inside_a_worker(board):
    _tool(_resume(board["target"]))
    with kbc.connect() as conn:
        with pytest.raises(ksr.SupervisorRequestError, match="worker_context"):
            ksr.drain(conn)
        assert kb.get_task(conn, board["target"]).status == "triage"


def test_registered_tool_schema_offers_resume():
    from tools.registry import registry
    import tools.kanban_tools
    schema = registry.get_schema("kanban_supervisor_request")
    assert "resume" in schema["parameters"]["properties"]["ops"]["items"]["properties"]["op"]["enum"]
