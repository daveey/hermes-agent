"""An outside kanban_schedule/kanban_block on a card a live worker owns is refused (t_01a1d01a0a065d6b). [daveey]

Incident 2026-11-12 (card t_01a1d017f26dc827): an orchestrating chat parked a card worker-2 had claimed seconds
earlier. The park closed the worker's open run underneath it; the worker exited rc=0 with no terminal kanban call
and the dispatcher booked a protocol violation. Now the outside park is refused with ``code=live_claim_park``
naming the claimer, and the worker's run stays open, so it can still park its own card.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_DB", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


def _conn():
    from hermes_cli import kanban_db_connect as kbc
    return kbc.connect()


def _claimed(live: bool = True) -> tuple[str, int]:
    """A card the dispatcher claimed; with ``live`` this test process stands in for its spawned worker."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    with _conn() as conn:
        tid = kb.create_task(conn, title="verify after owner lands", assignee="default")
        assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
        if live:
            kbd._set_worker_pid(conn, tid, os.getpid())
        return tid, kb._current_run_id(conn, tid)


def _run(run_id: int):
    with _conn() as conn:
        return conn.execute("SELECT ended_at, outcome FROM task_runs WHERE id = ?", (run_id,)).fetchone()


def _status(tid: str) -> str:
    from hermes_cli import kanban_db as kb
    with _conn() as conn:
        return kb.get_task(conn, tid).status


def _tool(name: str, args: dict) -> dict:
    from tools import kanban_tools as kt
    return json.loads(getattr(kt, name)(args))


def test_outside_schedule_of_a_live_claimed_card_is_refused_and_the_run_stays_open(home):
    tid, run_id = _claimed()
    out = _tool("_handle_schedule", {"task_id": tid, "reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z rate-limit cooldown",
                                     "clock_only": True})
    assert out.get("code") == "live_claim_park", out
    assert out.get("rejection") == "live_claim" and out.get("run_id") == run_id, out
    assert out.get("holder") and out["holder"] in out["error"], out
    assert "kanban_comment" in out["error"] and "Nothing changed" in out["error"], out
    assert _status(tid) == "running"
    assert _run(run_id)["ended_at"] is None  # the worker's run was not ended underneath it


def test_outside_block_of_a_live_claimed_card_is_refused(home):
    tid, run_id = _claimed()
    out = _tool("_handle_block", {"task_id": tid, "kind": "time", "reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                  "clock_only": True})
    assert out.get("code") == "live_claim_park", out
    assert _status(tid) == "running" and _run(run_id)["ended_at"] is None


def test_the_worker_still_parks_its_own_live_card(home, monkeypatch):
    tid, run_id = _claimed()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    out = _tool("_handle_schedule", {"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z rate-limit cooldown", "clock_only": True})
    assert out.get("ok") is True and out["status"] == "scheduled", out
    assert _run(run_id)["outcome"] == "scheduled"


def test_an_unclaimed_ready_card_is_still_parked_from_outside(home):
    from hermes_cli import kanban_db as kb
    with _conn() as conn:
        tid = kb.create_task(conn, title="not started", assignee="default")
    assert _status(tid) == "ready"
    out = _tool("_handle_schedule", {"task_id": tid, "reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z rate-limit cooldown",
                                     "clock_only": True})
    assert out.get("ok") is True and out["status"] == "scheduled", out


def test_a_claim_whose_worker_is_gone_can_still_be_parked_from_outside(home):
    """A claim with no live worker protects no run (the same rule ``complete_task`` applies)."""
    tid, run_id = _claimed(live=False)
    out = _tool("_handle_schedule", {"task_id": tid, "reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z rate-limit cooldown",
                                     "clock_only": True})
    assert out.get("ok") is True and out["status"] == "scheduled", out
    assert _run(run_id)["outcome"] == "scheduled"
