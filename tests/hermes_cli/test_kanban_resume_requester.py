"""Kernel-made recovery requester for continuation_exhausted parks. [daveey]

Reproduces layers t_01a19084a3b36956: parked continuation_exhausted in triage, no requester could pass
both the scope fence (<= 3 creator hops) and the freshness fence (created after the park), because
the parked card cannot create one and every older card predates the park. The dispatcher tick now
creates that requester. Every test runs on an isolated board under ``tmp_path``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_resume_exec as rx
from hermes_cli import kanban_resume_requester as rr
from hermes_cli import kanban_supervisor_requests as ksr
from hermes_cli import kanban_triage_sweep as ts

EXHAUSTED = "continuation_exhausted: split the work or reset the continuation budget"


def _exhaust(conn, tid):
    assert kb.claim_task(conn, tid) is not None
    run = kb._current_run_id(conn, tid)
    out = kc.continue_task(conn, tid, checkpoint="partial", expected_run_id=run, max_continuations=0)
    assert out["reason_code"] == "continuation_exhausted", out
    assert kb.block_task(conn, tid, kind="transient", reason=EXHAUSTED, expected_run_id=run)
    assert kb.get_task(conn, tid).status == "triage"


@pytest.fixture
def board(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    assert str(kb.kanban_db_path()).startswith(str(tmp_path))
    with kbc.connect() as conn:
        target = kb.create_task(conn, title="layers 2-8", assignee="default", created_by="default")
        _exhaust(conn, target)
    return {"target": target}


def _tick(conn):
    from hermes_cli import kanban_db_dispatch as kbd
    return kbd.dispatch_once(conn, max_spawn=0, spawn_fn=lambda *a, **k: None).supervisor_requests


def _recovery_cards(conn, target):
    return [r[0] for r in conn.execute(
        "SELECT id FROM tasks WHERE created_by = ? AND status != 'archived' ORDER BY "
        "(SELECT MIN(e.id) FROM task_events e WHERE e.task_id = tasks.id AND e.kind = 'created')",
        (rr.CREATED_BY,)) if ksr._created_by_task(conn, r[0]) == target]


def _as_worker(monkeypatch, conn, tid):
    assert kb.claim_task(conn, tid) is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(conn, tid)))


def _as_executor(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)


def _resume(tid):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_supervisor_request(
        {"ops": [{"op": "resume", "task_id": tid}], "reason": "progressing; needs more runs"}))


def _via_resume(conn, tid):
    return [e.payload for e in kb.list_events(conn, tid)
            if e.kind == "unblocked" and (e.payload or {}).get("via") == rx.VIA]


# --- new path --------------------------------------------------------------------------------

def test_tick_creates_one_fresh_in_scope_requester_and_its_resume_applies(board, monkeypatch):
    target = board["target"]
    with kbc.connect() as conn:
        park = rx._park_event_id(conn, target)
        _tick(conn)
        _tick(conn)  # idempotent: one card per park
        cards = _recovery_cards(conn, target)
        assert len(cards) == 1, cards
        rec = cards[0]
        task = kb.get_task(conn, rec)
        assert (task.status, task.assignee, task.created_by) == ("ready", "default", rr.CREATED_BY)
        assert target in task.body and "kanban_supervisor_request" in task.body
        assert conn.execute("SELECT idempotency_key FROM tasks WHERE id = ?", (rec,)).fetchone()[0] == \
            f"recovery:{target}:{park}"
        assert [e.payload for e in kb.list_events(conn, target) if e.kind == rr.EVENT] == [
            {"recovery_task_id": rec, "park_event_id": park}]
        # in scope (1 hop) and post-park by construction; every fence passes
        assert rx.refusal(conn, rec, target) is None
        assert kb.get_task(conn, target).status == "triage"  # creating the card moves nothing
        _as_worker(monkeypatch, conn, rec)
    out = _resume(target)
    assert out["ok"] and out["status"] == "pending", out
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert _tick(conn) == [(out["request_id"], "done")]
        req = ksr.get_request(conn, out["request_id"])
        assert req["status"] == "done" and req["result"][0]["outcome"] == "applied", req
        assert kb.get_task(conn, target).status == "ready"
        assert _via_resume(conn, target) == [{"via": rx.VIA, "request_id": out["request_id"], "by": rec}]
        _tick(conn)
        assert _recovery_cards(conn, target) == [rec]  # released: no new card


def test_a_new_park_gets_a_new_requester_and_the_old_one_is_stale(board, monkeypatch):
    target = board["target"]
    with kbc.connect() as conn:
        _tick(conn)
        (first,) = _recovery_cards(conn, target)
        _as_worker(monkeypatch, conn, first)
    out = _resume(target)
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert _tick(conn) == [(out["request_id"], "done")]
        _exhaust(conn, target)  # ran out of budget again
        assert "predates" in rx.refusal(conn, first, target)  # stale requester, not made for this park
        _tick(conn)
        cards = _recovery_cards(conn, target)
        assert len(cards) == 2 and cards[0] == first, cards
        assert rx.refusal(conn, cards[1], target) is None


# --- preserved refusals ----------------------------------------------------------------------

def test_human_gated_target_gets_no_requester_and_a_resume_is_refused(board, monkeypatch):
    target = board["target"]
    with kbc.connect() as conn:
        _tick(conn)
        (rec,) = _recovery_cards(conn, target)
        with kb.write_txn(conn):  # a human gate lands on the parked card after the requester exists
            conn.execute("UPDATE tasks SET block_kind = 'needs_input', human_request = ? WHERE id = ?",
                         (json.dumps({"action": "pick A or B"}), target))
        _as_worker(monkeypatch, conn, rec)
    out = _resume(target)
    assert out["code"] == "refused" and "human_gate" in out["error"], out
    with kbc.connect() as conn:
        assert kb.get_task(conn, target).status == "triage"
        gated = kb.create_task(conn, title="gated", assignee="default")
        _exhaust(conn, gated)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET block_kind = 'needs_input', human_request = ? WHERE id = ?",
                         (json.dumps({"action": "decide"}), gated))
        _as_executor(monkeypatch)
        _tick(conn)
        assert _recovery_cards(conn, gated) == []


def test_non_exhausted_park_gets_no_requester_and_is_refused(board, monkeypatch):
    with kbc.connect() as conn:
        other = kb.create_task(conn, title="flaky", assignee="default")
        assert kb.claim_task(conn, other) is not None
        assert kb.block_task(conn, other, kind="transient", reason="flaky network",
                             expected_run_id=kb._current_run_id(conn, other))
        _tick(conn)
        assert _recovery_cards(conn, other) == []
        child = kb.create_task(conn, title="fresh child", assignee="default", creator_task_id=other)
        assert "not a continuation_exhausted block" in rx.refusal(conn, child, other)


def test_requester_is_out_of_scope_for_any_other_card(board, monkeypatch):
    with kbc.connect() as conn:
        foreign = kb.create_task(conn, title="someone else's card", assignee="default")
        _exhaust(conn, foreign)
        _tick(conn)
        (rec,) = _recovery_cards(conn, board["target"])
        _as_worker(monkeypatch, conn, rec)
    out = _resume(foreign)
    assert out["code"] == "refused" and "creator chain" in out["error"], out
    with kbc.connect() as conn:
        assert kb.get_task(conn, foreign).status == "triage"


def test_spent_resume_budget_and_recovery_cards_get_no_requester(board, monkeypatch):
    target = board["target"]
    for _ in range(rx.MAX_RESUMES):
        with kbc.connect() as conn:
            _tick(conn)
            rec = _recovery_cards(conn, target)[-1]
            _as_worker(monkeypatch, conn, rec)
        out = _resume(target)
        _as_executor(monkeypatch)
        with kbc.connect() as conn:
            assert _tick(conn) == [(out["request_id"], "done")]
            _exhaust(conn, target)
    with kbc.connect() as conn:
        before = _recovery_cards(conn, target)
        _tick(conn)
        assert _recovery_cards(conn, target) == before  # budget spent: the request would be refused
        nested = kb.create_task(conn, title="recover", assignee="default", created_by=rr.CREATED_BY,
                                creator_task_id=target)
        _exhaust(conn, nested)  # a recovery card that itself exhausts gets no recursive card
        _tick(conn)
        assert _recovery_cards(conn, nested) == []


def test_triage_sweep_waits_while_the_recovery_card_is_open(board, monkeypatch):
    target = board["target"]
    with kbc.connect() as conn:
        _tick(conn)
        (rec,) = _recovery_cards(conn, target)
        far = 10 ** 10
        assert ts.sweep_stale_triage(conn, now=far) == []
        assert kb.get_task(conn, target).status == "triage"
        assert kb.claim_task(conn, rec) is not None
        assert kb.complete_task(conn, rec, result="split the work instead")
        assert ts.sweep_stale_triage(conn, now=far) == [target]  # its producer finished: normal sweep
