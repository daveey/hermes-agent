"""Shared external blockers: many cards parked behind one blocker, probed once. [daveey]

Repro of the waste this fixes: ~20 cards each hit the same GitHub Actions billing wall,
were bulk-unblocked, and each one then re-discovered (and re-polled) the same wall on its
own. With ``blocker_id`` the dispatcher holds every card behind the blocker and spawns one
probe; a probe that ends without the id clears it and releases the rest.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_shared_blockers as ksb
import tools.kanban_tools as kt

BID = "github-actions-billing"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "3600")  # fake pids must not "crash"
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD",
                "HERMES_SESSION_ID", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ksb, "probe_interval_seconds", lambda cfg=None: 1800)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


@pytest.fixture
def spawns(monkeypatch):
    spawned: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)

    return spawned, fake_spawn


def _blocked_behind(conn, title: str, blocker_id=BID) -> str:
    tid = kb.create_task(conn, title=title, assignee="default")
    assert kb.claim_task(conn, tid) is not None
    assert kb.block_task(conn, tid, reason="GitHub Actions refuses every job: billing",
                         kind="capability", blocker_id=blocker_id)
    return tid


def _age_runs(conn, seconds: int) -> None:
    """Shift every run and event into the past (as if ``seconds`` had elapsed)."""
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at = started_at - ?, "
                     "ended_at = CASE WHEN ended_at IS NULL THEN NULL ELSE ended_at - ? END", (seconds, seconds))
        conn.execute("UPDATE task_events SET created_at = created_at - ?", (seconds,))


def _spawned_ids(res) -> list[str]:
    return [row[0] for row in res.spawned]


def test_block_records_blocker_on_run_and_event(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "a", blocker_id="  GitHub Actions Billing! ")
        run = conn.execute("SELECT metadata FROM task_runs WHERE task_id = ?", (tid,)).fetchone()
        assert json.loads(run["metadata"]) == {"blocker_id": BID}
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked'",
                          (tid,)).fetchone()
        assert json.loads(ev["payload"])["blocker_id"] == BID
        # Control: a plain block stores no blocker metadata.
        plain = kb.create_task(conn, title="p", assignee="default")
        kb.claim_task(conn, plain)
        kb.block_task(conn, plain, reason="need a decision", kind="needs_input")
        assert conn.execute("SELECT metadata FROM task_runs WHERE task_id = ?", (plain,)).fetchone()[0] is None
        assert ksb.blocker_for_task(ksb.blocker_states(conn), plain) is None


def test_unblocked_cards_behind_open_blocker_get_one_probe(kanban_home, spawns):
    _spawned, fake_spawn = spawns
    with kbc.connect_closing() as conn:
        cards = [_blocked_behind(conn, f"card {i}") for i in range(3)]
        for tid in cards:
            assert kb.unblock_task(conn, tid)        # the human's bulk unblock
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert len(_spawned_ids(res)) == 1           # one probe, not three
        probe = _spawned_ids(res)[0]
        held = sorted(t for t, reason in res.respawn_guarded if reason == ksb.GUARD_REASON)
        assert held == sorted(set(cards) - {probe})
        # While the probe runs, nobody else spawns.
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert _spawned_ids(res) == []
        # Probe hits the wall again and re-asserts: the rest stay held within the interval.
        assert kb.block_task(conn, probe, reason="still refused", kind="capability", blocker_id=BID)
        # Probe re-assertion is not an unblock loop: no triage escalation.
        assert kb.get_task(conn, probe).status == "blocked"
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert _spawned_ids(res) == []
        assert {t for t, r in res.respawn_guarded if r == ksb.GUARD_REASON} == set(cards) - {probe}
        # Interval elapses: exactly one more probe.
        _age_runs(conn, 1900)
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert len(_spawned_ids(res)) == 1
        probe2 = _spawned_ids(res)[0]
        assert probe2 != probe
        # Probe 2 gets through (completes): blocker clears, remaining ready card spawns and the
        # still-blocked first probe is released automatically.
        assert kb.complete_task(conn, probe2, result="released fine")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res.shared_blocker_released == [probe]
        rest = set(cards) - {probe, probe2}
        assert set(_spawned_ids(res)) == rest | {probe}
        assert not [t for t, r in res.respawn_guarded if r == ksb.GUARD_REASON]
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                          (probe, ksb.CLEARED_EVENT)).fetchone()
        assert json.loads(ev["payload"])["cleared_by_task"] == probe2


def test_cleared_blocker_releases_blocked_cards_without_human_unblock(kanban_home, spawns):
    _spawned, fake_spawn = spawns
    with kbc.connect_closing() as conn:
        a, b = _blocked_behind(conn, "a"), _blocked_behind(conn, "b")
        _age_runs(conn, 60)
        kb.unblock_task(conn, a)                     # human retries just one card
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert _spawned_ids(res) == [a]
        assert kb.get_task(conn, b).status == "blocked"
        kb.complete_task(conn, a, result="CI ran")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert res.shared_blocker_released == [b]
        assert b in _spawned_ids(res)
        # Released card's context names who cleared it.
        assert "cleared" in " ".join(c.body for c in kb.list_comments(conn, b))


def test_neutral_outcomes_neither_clear_nor_assert(kanban_home):
    with kbc.connect_closing() as conn:
        a, b = _blocked_behind(conn, "a"), _blocked_behind(conn, "b")
        _age_runs(conn, 60)
        kb.unblock_task(conn, a)
        kb.claim_task(conn, a)
        with kb.write_txn(conn):                     # the probe crashes: says nothing about CI
            kb._end_run(conn, a, outcome="crashed", error="pid gone")
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (a,))
        st = ksb.blocker_states(conn)[BID]
        assert st.is_open and set(st.behind) == {a, b}
        # The crashed probe started recently, so it consumed this interval's probe.
        assert ksb.shared_blocker_guard(conn, b, interval=1800) == ksb.GUARD_REASON
        assert ksb.shared_blocker_guard(conn, b, interval=0) is None
        # A different blocker on the next run clears the first (the wall moved).
        kb.claim_task(conn, a)
        kb.block_task(conn, a, reason="other wall", kind="capability", blocker_id="provider-outage")
        states = ksb.blocker_states(conn)
        assert set(states[BID].behind) == {b} and states[BID].cleared_by[0] == a
        assert not states[BID].is_open


def test_no_blocker_no_guard(kanban_home, spawns):
    _spawned, fake_spawn = spawns
    with kbc.connect_closing() as conn:
        tids = [kb.create_task(conn, title=f"plain {i}", assignee="default") for i in range(3)]
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert sorted(_spawned_ids(res)) == sorted(tids)


def test_continue_tool_carries_blocker_and_context_names_probe(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        other = _blocked_behind(conn, "other")
        tid = kb.create_task(conn, title="waits on CI", assignee="default")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
        kbd._set_worker_pid(conn, tid, os.getpid())
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    bad = json.loads(kt._handle_continue({"checkpoint": "c", "blocker_id": 7}))
    assert "error" in bad
    out = json.loads(kt._handle_continue({"checkpoint": "fix on main; CI refused (billing)",
                                          "resume_after_seconds": 600, "blocker_id": BID}))
    assert out["status"] == "scheduled"
    with kbc.connect_closing() as conn:
        meta = json.loads(conn.execute("SELECT metadata FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0])
        assert meta["blocker_id"] == BID
        st = ksb.blocker_states(conn)[BID]
        assert set(st.behind) == {tid, other}
        ctx = kb.build_worker_context(conn, other)
        assert "Open shared blockers" in ctx and f"`{BID}` (2 card(s) parked)" in ctx
        assert f"This run is the probe for `{BID}`" in ctx


def test_block_tool_validates_and_reports_blocker(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        kb.claim_task(conn, tid)
        run_id = kb._current_run_id(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    assert "error" in json.loads(kt._handle_block({"reason": "r", "blocker_id": "!!!"}))
    out = json.loads(kt._handle_block({"reason": "1) fix billing", "kind": "capability",
                                       "blocker_id": BID}))
    assert out.get("blocker_id") == BID
    assert ksb.normalize_blocker_id("A b/C") == "a-b-c"
    assert ksb.normalize_blocker_id("x" * 100) == "x" * 64
