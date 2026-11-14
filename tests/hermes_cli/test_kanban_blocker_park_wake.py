"""Shared-blocker parks keep their way back, and only evidence clears the blocker. [daveey]

Repro of 2026-10-28 04:15-04:33 PDT: two cards called ``kanban_block`` with a
``SCHEDULED_UNTIL=`` reason, a wake_check and ``blocker_id``. ``block_task`` skipped the
timed park whenever ``blocker_id`` was set, so both landed plain ``blocked`` with no wake
and no probe row. A parent chat then re-parked one with ``kanban_schedule`` and no id;
``blocker_states`` read that run as "ended without the blocker" and cleared it, and
``release_cleared_blockers`` unblocked the other card although the wall still stood.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_shared_blockers as ksb
from hermes_cli import kanban_wake_check as kwc
from hermes_cli.kanban_continuation import pending_wake
import tools.kanban_tools as kt

BID = "github-askevepodcast-suspended"
WAKE = "exit 1"  # "not yet": a real probe would curl the account
AWAIT = {"kind": "external", "ref": "github-support-ticket-40037",
         "why": "GitHub support reinstates a suspended account after review"}


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "3600")
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD",
                "HERMES_SESSION_ID", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ksb, "probe_interval_seconds", lambda cfg=None: 1800)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _spawn_noop(task, workspace, board=None):
    return None


def _tomorrow() -> str:
    return (datetime.now() + timedelta(days=1)).replace(microsecond=0).isoformat()


def _as_worker(monkeypatch, conn, tid: str) -> None:
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, os.getpid())
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(conn, tid)))


def _as_parent(monkeypatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)


def _worker_block_behind(monkeypatch, conn, title: str, *, kind: str = "capability") -> str:
    """The 04:15 call: kanban_block with SCHEDULED_UNTIL + wake_check + blocker_id."""
    tid = kb.create_task(conn, title=title, assignee="default")
    _as_worker(monkeypatch, conn, tid)
    out = json.loads(kt._handle_block({
        "reason": f"SCHEDULED_UNTIL={_tomorrow()} Eve's GitHub account is suspended (403 on GET /user)",
        "kind": kind, "blocker_id": BID, "wake_check": WAKE, "awaiting": AWAIT,
    }))
    assert "error" not in out, out
    _as_parent(monkeypatch)
    return tid


def _has_way_back(conn, tid: str) -> bool:
    task = kb.get_task(conn, tid)
    if task.status not in ("blocked", "scheduled"):
        return True
    if task.status == "scheduled" and (pending_wake(conn, tid) or {}).get("wake_at"):
        return True
    if kwc.get_state(conn, tid):
        return True
    if kb.unsatisfied_parents(conn, tid):
        return True
    return bool(task.human_request)


def _run_meta(conn, run_id: int) -> dict:
    raw = conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
    return json.loads(raw or "{}")


# --- AC1: kanban_block keeps the timed park when blocker_id is set ------------------------

@pytest.mark.parametrize("kind", ["capability", "time"])
def test_block_with_blocker_and_scheduled_until_parks_with_wake(kanban_home, monkeypatch, kind):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="eve memory", assignee="default")
        _as_worker(monkeypatch, conn, tid)
        run_id = kb._current_run_id(conn, tid)
        until = _tomorrow()
    out = json.loads(kt._handle_block({
        "reason": f"SCHEDULED_UNTIL={until} account suspended; wakes when /-/memory.md returns 200",
        "kind": kind, "blocker_id": BID, "wake_check": WAKE, "awaiting": AWAIT,
    }))
    assert "error" not in out, out
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled"
        wake = pending_wake(conn, tid)
        assert wake and wake["wake_at"] == int(datetime.fromisoformat(until).timestamp())
        assert wake.get("blocker_id") == BID
        state = kwc.get_state(conn, tid)
        assert state is not None and state["cmd"] == WAKE
        assert _run_meta(conn, run_id).get("blocker_id") == BID
        assert tid in ksb.blocker_states(conn)[BID].behind


def test_block_with_blocker_and_no_wake_is_given_one(kanban_home, monkeypatch):
    """AC3 for the blocker path: a blocker park with no timed marker still gets a wake (the
    shared probe cadence), so a board whose every card is parked behind X still probes X."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ci", assignee="default")
        _as_worker(monkeypatch, conn, tid)
    out = json.loads(kt._handle_block({"reason": "Actions refuses jobs: billing", "kind": "capability",
                                       "blocker_id": "github-actions-billing"}))
    assert "error" not in out, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "scheduled"
        wake = pending_wake(conn, tid)
        assert wake and 0 < wake["wake_at"] - time.time() <= 1800 + 5
        assert _has_way_back(conn, tid)
        # Kernel path (CLI/dispatcher callers) behaves the same.
        k = kb.create_task(conn, title="k", assignee="default")
        kb.claim_task(conn, k)
        assert kb.block_task(conn, k, reason="billing", kind="capability", blocker_id="github-actions-billing")
        assert kb.get_task(conn, k).status == "scheduled" and _has_way_back(conn, k)


def test_blocker_park_with_human_request_stays_a_visible_block(kanban_home):
    hr = {"action": "Raise the Actions spending limit", "why_human": "billing owner only",
          "interaction": "external", "scope": "github org billing", "evidence": "job annotation"}
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="h", assignee="default")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="billing", kind="needs_input",
                             blocker_id="github-actions-billing", human_request=hr)
        assert kb.get_task(conn, tid).status == "blocked" and _has_way_back(conn, tid)


# --- AC2: a re-park without the id does not clear an open blocker -------------------------

def _repark(monkeypatch, op: str, who: str, tid: str) -> None:
    until = _tomorrow()
    with kbc.connect_closing() as conn:
        # The card wakes (wake_at / probe cadence) and sits ready, held by the guard.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        conn.commit()
        if who == "worker":
            _as_worker(monkeypatch, conn, tid)   # it is the probe
        else:
            _as_parent(monkeypatch)              # the parent chat acts on it
    args = {} if who == "worker" else {"task_id": tid}
    if op == "schedule":
        out = kt._handle_schedule({**args, "reason": f"SCHEDULED_UNTIL={until} still suspended",
                                   "wake_check": WAKE, "awaiting": AWAIT})
    elif op == "block":
        out = kt._handle_block({**args, "reason": f"SCHEDULED_UNTIL={until} still suspended", "kind": "time",
                                "wake_check": WAKE, "awaiting": AWAIT})
    else:
        out = kt._handle_continue({**args, "checkpoint": "still suspended; re-check later",
                                   "resume_after_seconds": 3600, "wake_check": WAKE, "awaiting": AWAIT})
    assert "error" not in json.loads(out), out
    _as_parent(monkeypatch)


@pytest.mark.parametrize("op,who", [
    ("schedule", "parent"), ("schedule", "worker"),
    ("block", "parent"), ("block", "worker"),
    ("continue", "worker"),
])
def test_repark_without_blocker_id_keeps_blocker_open(kanban_home, monkeypatch, op, who):
    """The 04:28 sequence: a, b behind X; a is re-parked without the id; b must stay parked."""
    with kbc.connect_closing() as conn:
        a = _worker_block_behind(monkeypatch, conn, "a")
        b = _worker_block_behind(monkeypatch, conn, "b")
    _repark(monkeypatch, op, who, a)
    with kbc.connect_closing() as conn:
        st = ksb.blocker_states(conn)[BID]
        assert st.is_open, "a re-park without blocker_id cleared the blocker"
        assert set(st.behind) == {a, b}
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_noop)
        assert b not in (res.shared_blocker_released or [])
        assert kb.get_task(conn, b).status == "scheduled"
        assert not [e for e in kb.list_events(conn, b) if e.kind == ksb.CLEARED_EVENT]


def test_completion_clears_and_wakes_parked_cards(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        a = _worker_block_behind(monkeypatch, conn, "a")
        b = _worker_block_behind(monkeypatch, conn, "b")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (a,))
        conn.commit()
        kb.claim_task(conn, a)
        assert kb.complete_task(conn, a, result="account reinstated")
        assert not ksb.blocker_states(conn)[BID].is_open
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_noop)
        assert b in res.shared_blocker_released
        assert kb.get_task(conn, b).status != "scheduled"
        ev = [e for e in kb.list_events(conn, b) if e.kind == ksb.CLEARED_EVENT]
        assert ev and ev[0].payload["cleared_by_task"] == a


@pytest.mark.parametrize("current_blocker", [None, "other-wall", BID, "awaiting-only", "no-probe"])
def test_clear_respects_current_park(kanban_home, monkeypatch, current_blocker):
    with kbc.connect_closing() as conn:
        clearing = _worker_block_behind(monkeypatch, conn, "wall probe")
        waiting = _worker_block_behind(monkeypatch, conn, "lane waiter")
    if current_blocker != BID:
        _repark(monkeypatch, "schedule", "parent", waiting)
    with kbc.connect_closing() as conn:
        if current_blocker in ("other-wall", "awaiting-only"):
            with kb.write_txn(conn):
                wake = pending_wake(conn, waiting)
                wake["awaiting"]["shared_blocker"] = BID if current_blocker == "awaiting-only" else current_blocker
                kb._append_event(conn, waiting, "scheduled", wake)
        if current_blocker == "no-probe":
            with kb.write_txn(conn):
                kwc.set_check(conn, waiting, None, None)
        before = kwc.get_state(conn, waiting)
        park = pending_wake(conn, waiting)
        comments = kb.list_comments(conn, waiting)
        assert kb.unblock_task(conn, clearing)
        assert kb.claim_task(conn, clearing)
        assert kb.complete_task(conn, clearing, result="wall gone")
        released = ksb.release_cleared_blockers(conn)
        if current_blocker in (BID, "awaiting-only", "no-probe"):
            assert released == [waiting]
            assert kb.get_task(conn, waiting).status == "ready"
        else:
            assert released == []
            assert kb.get_task(conn, waiting).status == "scheduled"
            assert kwc.get_state(conn, waiting) == before
            assert pending_wake(conn, waiting) == park
            assert park["wake_at"] and before["cmd"] == WAKE
            assert kb.list_comments(conn, waiting) == comments
        events = [e for e in kb.list_events(conn, waiting) if e.kind == ksb.CLEARED_EVENT]
        assert len(events) == 1
        assert events[0].payload["cleared_by_task"] == clearing
        assert ksb.release_cleared_blockers(conn) == []


def test_explicit_clear_blocker_opt_out(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        a = _worker_block_behind(monkeypatch, conn, "a")
        b = _worker_block_behind(monkeypatch, conn, "b")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (a,))
        conn.commit()
    out = json.loads(kt._handle_schedule({"task_id": a, "reason": "account is back; waiting on a deploy now", "clock_only": True,
                                          "clear_blocker": True}))
    assert "error" not in out, out
    with kbc.connect_closing() as conn:
        assert not ksb.blocker_states(conn)[BID].is_open
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_noop)
        assert b in res.shared_blocker_released


def test_wake_check_success_on_a_card_behind_clears(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        a = _worker_block_behind(monkeypatch, conn, "a")
        b = _worker_block_behind(monkeypatch, conn, "b")
        state = kwc.get_state(conn, a)
        assert kwc.apply_result(conn, a, state["cmd"], 0, "200 OK", generation=state["generation"]) \
            == kwc.CONDITION_MET_EVENT
        st = ksb.blocker_states(conn)[BID]
        assert not st.is_open and st.cleared_by[0] == a
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_noop)
        assert b in res.shared_blocker_released


# --- AC3: no park path leaves a card with no way back -------------------------------------

@pytest.mark.parametrize("args", [
    {"reason": "billing", "kind": "capability", "blocker_id": "x-wall"},
    {"reason": "billing", "kind": "transient", "blocker_id": "x-wall"},
    {"reason": "billing", "kind": "dependency", "blocker_id": "x-wall"},
    {"reason": "billing", "blocker_id": "x-wall"},
    {"reason": "billing", "kind": "time", "blocker_id": "x-wall", "clock_only": True},
    {"reason": "billing", "kind": "needs_input", "blocker_id": "x-wall"},
    {"reason": "billing", "kind": "capability"},
    {"reason": "billing", "kind": "needs_input"},
    {"reason": "a soak", "kind": "time", "clock_only": True},
])
def test_every_block_park_has_a_way_back_or_is_refused(kanban_home, monkeypatch, args):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="p", assignee="default")
        _as_worker(monkeypatch, conn, tid)
    out = json.loads(kt._handle_block(dict(args)))
    with kbc.connect_closing() as conn:
        if "error" in out:
            assert out.get("code") or out.get("reason_code") or out["error"]
            assert kb.get_task(conn, tid).status == "running"
            return
        assert _has_way_back(conn, tid), (args, kb.get_task(conn, tid).status)


def test_park_invariant_kernel_refuses_blocker_park_it_cannot_wake(kanban_home, monkeypatch):
    """The blocked/untyped classify-in-place path with a blocker gets a wake too."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="breaker", assignee="default")
        conn.execute("UPDATE tasks SET status = 'blocked', block_kind = NULL, current_run_id = NULL "
                     "WHERE id = ?", (tid,))
        conn.commit()
        assert kb.block_task(conn, tid, reason="billing", kind="capability", blocker_id="x-wall")
        assert _has_way_back(conn, tid), kb.get_task(conn, tid).status


# --- A woken card that re-parks without the id is not re-woken every tick -----------------
# Repro of 2026-10-28 13:57-14:10 PDT: t_01a180e8c9732286 was woken by
# ``shared_blocker_cleared`` four times in ~12 min. Each time it re-parked without the id, so it
# stayed behind the (cleared) blocker and the next tick woke it again.

def _clear_via_wake_check(conn, tid: str) -> None:
    state = kwc.get_state(conn, tid)
    assert kwc.apply_result(conn, tid, state["cmd"], 0, "200 OK", generation=state["generation"]) \
        == kwc.CONDITION_MET_EVENT


def _cleared_events(conn, tid: str) -> list:
    return [e for e in kb.list_events(conn, tid) if e.kind == ksb.CLEARED_EVENT]


def test_woken_card_that_reparks_without_id_after_running_is_not_rewoken(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        a = _worker_block_behind(monkeypatch, conn, "a")
        b = _worker_block_behind(monkeypatch, conn, "b")
        _clear_via_wake_check(conn, b)                   # b's own blocker park + probe: real evidence
        assert not ksb.blocker_states(conn)[BID].is_open
        assert ksb.release_cleared_blockers(conn) == [a]   # a woken once
        assert kb.get_task(conn, a).status == "ready"
        # a runs, finds the wall still up, and re-parks WITHOUT the id (a "pass").
        _as_worker(monkeypatch, conn, a)
    out = json.loads(kt._handle_continue({"checkpoint": "API still 404; re-check later",
                                          "resume_after_seconds": 3600, "wake_check": WAKE, "awaiting": AWAIT}))
    assert "error" not in out, out
    _as_parent(monkeypatch)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, a).status == "scheduled"
        assert ksb.release_cleared_blockers(conn) == []
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_noop)
        assert a not in (res.shared_blocker_released or [])
        assert kb.get_task(conn, a).status == "scheduled"
        assert len(_cleared_events(conn, a)) == 1


def test_woken_card_reparked_by_parent_before_running_is_not_rewoken(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        a = _worker_block_behind(monkeypatch, conn, "a")
        b = _worker_block_behind(monkeypatch, conn, "b")
        _clear_via_wake_check(conn, b)
        assert ksb.release_cleared_blockers(conn) == [a]
    out = json.loads(kt._handle_schedule({"task_id": a, "reason": f"SCHEDULED_UNTIL={_tomorrow()} still 404",
                                          "wake_check": WAKE, "awaiting": AWAIT}))
    assert "error" not in out, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, a).status == "scheduled"
        assert ksb.release_cleared_blockers(conn) == []
        assert len(_cleared_events(conn, a)) == 1


def test_newer_clear_after_an_independent_repark_retires_only_the_hold(kanban_home, monkeypatch):
    """A new assertion episode does not take over the card's independent park."""
    with kbc.connect_closing() as conn:
        a = _worker_block_behind(monkeypatch, conn, "a")
        b = _worker_block_behind(monkeypatch, conn, "b")
        _clear_via_wake_check(conn, b)
        assert ksb.release_cleared_blockers(conn) == [a]
    out = json.loads(kt._handle_schedule({"task_id": a, "reason": f"SCHEDULED_UNTIL={_tomorrow()} still 404",
                                          "wake_check": WAKE, "awaiting": AWAIT}))
    assert "error" not in out, out
    with kbc.connect_closing() as conn:
        _age_all(conn, 120)
        c = _worker_block_behind(monkeypatch, conn, "c")     # X re-asserted by a third card ...
        _clear_via_wake_check(conn, c)                      # ... and cleared again, later
        assert ksb.blocker_states(conn)[BID].cleared_by[0] == c
        before = pending_wake(conn, a), kwc.get_state(conn, a), _dispatcher_comments(conn, a)
        assert a not in ksb.release_cleared_blockers(conn)
        assert kb.get_task(conn, a).status == "scheduled"
        assert (pending_wake(conn, a), kwc.get_state(conn, a), _dispatcher_comments(conn, a)) == before
        assert len(_cleared_events(conn, a)) == 2


def _age_all(conn, seconds: int) -> None:
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at = started_at - ?, "
                     "ended_at = CASE WHEN ended_at IS NULL THEN NULL ELSE ended_at - ? END", (seconds, seconds))
        conn.execute("UPDATE task_events SET created_at = created_at - ?", (seconds,))
        conn.execute("UPDATE task_comments SET created_at = created_at - ?", (seconds,))


# --- A redundant clear does not re-wake a card an earlier clear already released ----------
# Repro of 2026-10-28 15:41-15:53 PDT: t_e7dc822c sat behind github-askevepodcast-suspended.
# t_01a1800921b1ff50 cleared it (15:41, correct wake); t_e7dc822c re-parked without the id;
# t_01a180e8c9732286 then also ended a run with clear_blocker=true (15:53, no re-assertion in
# between), and t_e7dc822c was woken a second time for no new information.

def _run_and_clear(monkeypatch, tid: str) -> None:
    """``tid`` runs, sees the wall gone, and parks for another reason with clear_blocker=true."""
    with kbc.connect_closing() as conn:
        if kb.get_task(conn, tid).status != "ready":
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
            conn.commit()
        _as_worker(monkeypatch, conn, tid)
    out = json.loads(kt._handle_schedule({"reason": "account is back; waiting on a deploy now",
                                          "clock_only": True, "clear_blocker": True}))
    assert "error" not in out, out
    _as_parent(monkeypatch)


def _repark_without_id(monkeypatch, tid: str) -> None:
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        _as_worker(monkeypatch, conn, tid)
    out = json.loads(kt._handle_continue({"checkpoint": "API still 404 for me; re-check later",
                                          "resume_after_seconds": 3600, "wake_check": WAKE, "awaiting": AWAIT}))
    assert "error" not in out, out
    _as_parent(monkeypatch)


def _dispatcher_comments(conn, tid: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ? AND author = 'dispatcher'",
                        (tid,)).fetchone()[0]


def _two_clears_in_sequence(monkeypatch) -> tuple[str, str, str]:
    """A, B, C behind X; A clears (C woken once); C re-parks without the id; B clears again."""
    with kbc.connect_closing() as conn:
        a = _worker_block_behind(monkeypatch, conn, "a")
        b = _worker_block_behind(monkeypatch, conn, "b")
        c = _worker_block_behind(monkeypatch, conn, "c")
        _age_all(conn, 600)
    _run_and_clear(monkeypatch, a)
    with kbc.connect_closing() as conn:
        st = ksb.blocker_states(conn)[BID]
        assert not st.is_open and st.cleared_by[0] == a
        released = ksb.release_cleared_blockers(conn)
        assert c in released and b in released
        assert len(_cleared_events(conn, c)) == 1 and _dispatcher_comments(conn, c) == 1
    _repark_without_id(monkeypatch, c)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, c).status == "scheduled"
        _age_all(conn, 120)
    _run_and_clear(monkeypatch, b)
    with kbc.connect_closing() as conn:
        assert ksb.blocker_states(conn)[BID].cleared_by[0] == b   # newest clear, kept for reporting
    return a, b, c


def test_redundant_clear_by_another_card_does_not_rewake(kanban_home, monkeypatch):
    _a, _b, c = _two_clears_in_sequence(monkeypatch)
    with kbc.connect_closing() as conn:
        assert ksb.release_cleared_blockers(conn) == []
        assert kb.get_task(conn, c).status == "scheduled"
        assert len(_cleared_events(conn, c)) == 1
        assert _dispatcher_comments(conn, c) == 1
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_noop)
        assert c not in (res.shared_blocker_released or [])
        assert len(_cleared_events(conn, c)) == 1


def test_clear_after_reassertion_keeps_independent_park_after_redundant_clear(kanban_home, monkeypatch):
    _a, _b, c = _two_clears_in_sequence(monkeypatch)
    with kbc.connect_closing() as conn:
        assert ksb.release_cleared_blockers(conn) == []
        _age_all(conn, 120)
        d = _worker_block_behind(monkeypatch, conn, "d")      # X re-asserted ...
        assert ksb.blocker_states(conn)[BID].is_open
        _age_all(conn, 60)
        _clear_via_wake_check(conn, d)                      # ... then cleared by a run's probe
        st = ksb.blocker_states(conn)[BID]
        assert not st.is_open and st.cleared_by[0] == d
        before = pending_wake(conn, c), kwc.get_state(conn, c), _dispatcher_comments(conn, c)
        assert c not in ksb.release_cleared_blockers(conn)
        assert kb.get_task(conn, c).status == "scheduled"
        assert (pending_wake(conn, c), kwc.get_state(conn, c), _dispatcher_comments(conn, c)) == before
        assert len(_cleared_events(conn, c)) == 2


# --- A wake_check clears X only when the park that set it carried X -----------------------
# Repro of 2026-10-28 13:22-14:05 PDT: t_01a1800921b1ff50 was re-parked (no blocker_id) with
# `ac8-live.sh || watch_eve.py ...`; the Slack half exited 0 on every new message, and each exit 0
# was read as "github-askevepodcast-suspended is gone" (six false clears, API 404 throughout).

OR_PROBE = "./ac8-live.sh || ./watch_eve.py --new-since-last"


@pytest.mark.parametrize("op", ["schedule", "continue"])
def test_or_probe_set_by_a_park_without_the_id_does_not_clear(kanban_home, monkeypatch, op):
    with kbc.connect_closing() as conn:
        a = _worker_block_behind(monkeypatch, conn, "a")
        b = _worker_block_behind(monkeypatch, conn, "b")
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (a,))
        conn.commit()
        if op == "continue":
            _as_worker(monkeypatch, conn, a)
    if op == "schedule":
        out = kt._handle_schedule({"task_id": a, "reason": f"SCHEDULED_UNTIL={_tomorrow()} still suspended",
                                   "wake_check": OR_PROBE, "awaiting": AWAIT})
    else:
        out = kt._handle_continue({"checkpoint": "still suspended", "resume_after_seconds": 3600,
                                   "wake_check": OR_PROBE, "awaiting": AWAIT})
    assert "error" not in json.loads(out), out
    _as_parent(monkeypatch)
    with kbc.connect_closing() as conn:
        assert kwc.get_state(conn, a)["cmd"] == OR_PROBE
        _clear_via_wake_check(conn, a)              # the Slack half exits 0
        assert kb.get_task(conn, a).status != "scheduled"   # a itself still wakes on its own probe
        st = ksb.blocker_states(conn)[BID]
        assert st.is_open, "a probe set by a park without blocker_id cleared the blocker"
        assert ksb.release_cleared_blockers(conn) == []
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_noop)
        assert b not in (res.shared_blocker_released or [])
        assert kb.get_task(conn, b).status == "scheduled"
        assert not _cleared_events(conn, b)


def test_wake_check_schema_says_exit_0_must_test_only_the_wall():
    from tools import kanban_tools_schemas as kts
    desc = kts._WAKE_CHECK_PROPS["wake_check"]["description"]
    assert "blocker_id" in desc and "only the wall" in desc
