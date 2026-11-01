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
def scoped_routing(monkeypatch):
    from hermes_cli import kanban_model_tier as tier
    from hermes_cli import kanban_model_assignment as assignment
    monkeypatch.setattr(tier, "load_tier_config", lambda: {
        "default_model": "gpt-5", "default_provider": "openai-codex",
        "strong_model": "claude-opus-4", "strong_provider": "anthropic",
    })
    monkeypatch.setattr(assignment, "_assignee_config", lambda _: {
        "model": {"default": "gpt-5", "provider": "openai-codex"}})


def _scope(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET metadata = ? WHERE task_id = ?", (json.dumps({
            "blocker_id": BID, "blocker_provider": "anthropic",
            "blocker_model": "claude-sonnet-4", "blocker_lane": "review",
        }), tid))


def _route_run(conn, tid, provider):
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "model_tier", {
            "model": "gpt-5" if provider == "openai-codex" else "claude-opus-4",
            "provider": provider, "lane": "ready",
        }, run_id=kb._current_run_id(conn, tid))


def test_scoped_next_lane_and_context(kanban_home, scoped_routing):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "scoped")
        _scope(conn, tid)
        st = ksb.blocker_states(conn)[BID]
        assert (st.blocker_provider, st.blocker_model, st.blocker_lane) == (
            "anthropic", "claude-sonnet-4", "review")
        assert ksb.shared_blocker_guard(conn, tid, interval=1800) is None
        ctx = ksb.render_blocker_context(conn, tid)
        assert "provider=openai-codex" in ctx and "model=gpt-5" in ctx and "lane=ready" in ctx
        assert "not applicable" in ctx and "This run is the probe" not in ctx
        assert "later" in ctx
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        assert ksb.shared_blocker_guard(conn, tid, interval=1800) == ksb.GUARD_REASON
        ctx = ksb.render_blocker_context(conn, tid)
        assert "provider=anthropic" in ctx and "model=claude-opus-4" in ctx and "lane=review" in ctx
        assert "This run is the probe" in ctx


@pytest.mark.parametrize("finish", [False, True])
def test_other_provider_does_not_probe_or_clear(kanban_home, scoped_routing, finish):
    with kbc.connect_closing() as conn:
        a, b = _blocked_behind(conn, "a"), _blocked_behind(conn, "b")
        _scope(conn, a)
        _scope(conn, b)
        _age_runs(conn, 1900)
        kb.unblock_task(conn, a)
        kb.claim_task(conn, a)
        _route_run(conn, a, "openai-codex")
        if finish:
            kb.complete_task(conn, a, result="implementation done")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (b,))
        st = ksb.blocker_states(conn)[BID]
        assert st.is_open and a not in st.probe_starts
        assert ksb.shared_blocker_guard(conn, b, interval=1800) is None


def test_matching_provider_completion_clears(kanban_home, scoped_routing):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "a")
        _scope(conn, tid)
        _age_runs(conn, 60)
        kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid)
        _route_run(conn, tid, "anthropic")
        kb.complete_task(conn, tid, result="wall gone")
        assert not ksb.blocker_states(conn)[BID].is_open


def test_scheduled_review_uses_resume_lane(kanban_home, scoped_routing):
    from hermes_cli.kanban_continuation import continue_task
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="review", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        assert kb.claim_review_task(conn, tid)
        assert continue_task(conn, tid, checkpoint="provider unavailable", resume_after_seconds=600,
                             metadata={"blocker_id": BID, "blocker_provider": "anthropic"})["ok"]
        assert ksb.task_route(conn, tid) == ("claude-opus-4", "anthropic", "review")
        assert ksb.shared_blocker_guard(conn, tid, interval=1800) == ksb.GUARD_REASON


@pytest.mark.parametrize("provider", [None, "auto", "anthropic"])
def test_unknown_and_matching_provider_still_apply(kanban_home, scoped_routing, provider):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "scoped")
        _scope(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET model_override=?, provider_override=? WHERE id=?",
                         ("class:deep" if provider == "auto" else "custom-model", provider, tid))
        assert ksb.shared_blocker_guard(conn, tid, interval=1800) == ksb.GUARD_REASON


def test_stranded_probe_ignores_other_provider_running(kanban_home, scoped_routing):
    with kbc.connect_closing() as conn:
        a = _blocked_behind(conn, "unrelated")
        b = _legacy_blocked_behind(conn, "stranded")
        _scope(conn, a)
        _scope(conn, b)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET model_override='claude-opus-4', provider_override='anthropic' WHERE id=?", (b,))
        _age_runs(conn, 1900)
        kb.unblock_task(conn, a)
        kb.claim_task(conn, a)
        _route_run(conn, a, "openai-codex")
        assert ksb.probe_stranded_blockers(conn, interval=1800) == [b]


def test_historical_route_not_replaced_by_current_pin(kanban_home, scoped_routing):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "unrelated")
        _scope(conn, tid)
        _age_runs(conn, 1900)
        kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid)
        _route_run(conn, tid, "openai-codex")
        with kb.write_txn(conn):
            kb._end_run(conn, tid, outcome="review_requested")
            conn.execute("UPDATE tasks SET status='review', model_override='claude-opus-4', "
                         "provider_override='anthropic' WHERE id=?", (tid,))
        st = ksb.blocker_states(conn)[BID]
        assert st.is_open and not st.probe_starts
        assert ksb.task_route(conn, tid) == ("claude-opus-4", "anthropic", "review")


def test_profile_default_when_tiering_off(kanban_home, scoped_routing, monkeypatch):
    from hermes_cli import kanban_model_tier as tier
    monkeypatch.setattr(tier, "load_tier_config", lambda: {})
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "profile")
        _scope(conn, tid)
        assert ksb.task_route(conn, tid) == ("gpt-5", "openai-codex", "ready")
        assert ksb.shared_blocker_guard(conn, tid) is None


def test_running_other_provider_before_tier_receipt_is_not_probe(kanban_home, scoped_routing):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "running")
        _scope(conn, tid)
        kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid)
        assert tid not in ksb.blocker_states(conn)[BID].probe_starts


def test_retry_uses_strong_tier_without_writing_routing(kanban_home, scoped_routing):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "retry")
        _scope(conn, tid)
        kb.unblock_task(conn, tid)
        kb.claim_task(conn, tid)
        with kb.write_txn(conn):
            kb._end_run(conn, tid, outcome="crashed")
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        count = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        assert ksb.task_route(conn, tid) == ("claude-opus-4", "anthropic", "ready")
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == count
        task = kb.get_task(conn, tid)
        assert task is not None and task.model_override is None


def test_profile_class_provider_remains_unknown(kanban_home, scoped_routing, monkeypatch):
    from hermes_cli import kanban_model_tier as tier, kanban_model_assignment as assignment
    monkeypatch.setattr(tier, "load_tier_config", lambda: {})
    monkeypatch.setattr(assignment, "_assignee_config", lambda _: {
        "model": {"default": "class:standard", "provider": "openai-codex"}})
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "class")
        _scope(conn, tid)
        assert ksb.task_route(conn, tid) == ("class:standard", None, "ready")
        assert ksb.shared_blocker_guard(conn, tid) == ksb.GUARD_REASON


def test_unscoped_wall_still_holds_other_provider(kanban_home, scoped_routing):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "legacy")
        assert ksb.shared_blocker_guard(conn, tid, interval=1800) == ksb.GUARD_REASON



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


def _legacy_blocked_behind(conn, title: str, blocker_id=BID) -> str:
    """A card parked behind the blocker before blocker parks got their own wake: plain
    ``blocked`` with no wake (the shape of cards parked before the fix, which only
    ``probe_stranded_blockers`` can bring back)."""
    from unittest import mock

    from hermes_cli import kanban_timed_park as ktp
    with mock.patch.object(ktp, "park_mode", lambda *a, **k: None):   # the pre-fix kernel
        tid = _blocked_behind(conn, title, blocker_id=blocker_id)
    assert kb.get_task(conn, tid).status == "blocked"
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
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'scheduled')",
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
        assert kb.get_task(conn, probe).status == "scheduled"
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
        assert kb.get_task(conn, b).status == "scheduled"
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


# --- a park with blocker_id must keep a way to wake (t_01a18246d7ae8a78) -------------------------

def _future_iso(seconds: int) -> str:
    from datetime import UTC, datetime
    return datetime.fromtimestamp(int(time.time()) + seconds, tz=UTC).isoformat()


def test_timed_park_with_blocker_keeps_wake_and_probe(kanban_home):
    with kbc.connect_closing() as conn:
        other = _blocked_behind(conn, "other")
        tid = kb.create_task(conn, title="waits on CI window", assignee="default")
        kb.claim_task(conn, tid)
        reason = f"SCHEDULED_UNTIL={_future_iso(36 * 3600)} watcher wakes it"
        assert kb.block_task(conn, tid, reason=reason, kind="time", blocker_id=BID, wake_check="exit 1")
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled"
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'scheduled' "
                          "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        payload = json.loads(ev["payload"])
        assert payload["wake_at"] > time.time() + 30 * 3600
        assert payload["blocker_id"] == BID
        row = conn.execute("SELECT cmd FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()
        assert row is not None and row["cmd"] == "exit 1"
        st = ksb.blocker_states(conn)[BID]
        assert st.is_open and set(st.behind) == {tid, other}


def test_planned_wake_counts_blocker_parks(kanban_home):
    from hermes_cli import kanban_wake_check as kwc
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        kb.claim_task(conn, tid)
        wake = kwc.planned_wake(conn, tid, op="block", kind="time", blocker_id=BID,
                                reason=f"SCHEDULED_UNTIL={_future_iso(5 * 3600)}")
        assert wake is not None and wake > time.time() + 4 * 3600


def test_wake_check_on_agent_owned_block_is_honored_not_dropped(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="billing", kind="capability", blocker_id=BID,
                             wake_check="exit 1")
        assert kb.get_task(conn, tid).status == "scheduled"
        assert conn.execute("SELECT cmd FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone()["cmd"] == "exit 1"
        assert tid in ksb.blocker_states(conn)[BID].behind


def test_wake_check_on_human_ask_block_is_refused_not_dropped(kanban_home):
    hr = {"action": "raise the GitHub spending limit", "why_human": "billing owner",
          "interaction": "approval", "scope": "org billing", "evidence": "job annotation"}
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="default")
        kb.claim_task(conn, tid)
        with pytest.raises(ValueError, match="wake_check"):
            kb.block_task(conn, tid, reason="billing", kind="needs_input", blocker_id=BID,
                          wake_check="exit 1", human_request=hr)
        assert kb.get_task(conn, tid).status == "running"
        assert conn.execute("SELECT 1 FROM task_wake_checks WHERE task_id = ?", (tid,)).fetchone() is None


def test_lone_blocked_card_behind_open_blocker_is_probed_after_interval(kanban_home, spawns):
    _spawned, fake_spawn = spawns
    with kbc.connect_closing() as conn:
        tid = _legacy_blocked_behind(conn, "alone")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert _spawned_ids(res) == []                 # within the interval: stays blocked
        assert kb.get_task(conn, tid).status == "blocked"
        _age_runs(conn, 1900)
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert _spawned_ids(res) == [tid]               # the interval passed: it is the probe
        assert conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = ?",
                            (tid, ksb.PROBE_RELEASED_EVENT)).fetchone()


def test_all_blocked_behind_blocker_get_exactly_one_probe(kanban_home, spawns):
    _spawned, fake_spawn = spawns
    with kbc.connect_closing() as conn:
        cards = [_legacy_blocked_behind(conn, f"c{i}") for i in range(3)]
        _age_runs(conn, 1900)
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert len(_spawned_ids(res)) == 1
        probe = _spawned_ids(res)[0]
        assert [kb.get_task(conn, t).status for t in cards if t != probe] == ["blocked", "blocked"]
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)    # probe in flight: nothing more
        assert _spawned_ids(res) == []
        # The probe re-asserts: no new probe until the interval passes again.
        assert kb.block_task(conn, probe, reason="still refused", kind="capability", blocker_id=BID)
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert _spawned_ids(res) == []


def test_human_ask_behind_blocker_is_not_auto_probed(kanban_home, spawns):
    _spawned, fake_spawn = spawns
    hr = {"action": "raise the GitHub spending limit", "why_human": "billing owner",
          "interaction": "approval", "scope": "org billing", "evidence": "job annotation"}
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="needs David", assignee="default")
        kb.claim_task(conn, tid)
        assert kb.block_task(conn, tid, reason="billing", kind="needs_input", blocker_id=BID,
                             human_request=hr)
        _age_runs(conn, 1900)
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert _spawned_ids(res) == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_blocker_wake_and_stranded_probe_together_yield_one_probe(kanban_home, spawns):
    """A legacy ``blocked`` card and a blocker-wake ``scheduled`` card behind the same blocker:
    once the scheduled card's wake is due, the two wake paths still start ONE probe. [daveey]"""
    from hermes_cli.kanban_continuation import wake_due_continuations
    _spawned, fake_spawn = spawns
    with kbc.connect_closing() as conn:
        legacy = _legacy_blocked_behind(conn, "legacy")
        parked = _blocked_behind(conn, "parked")
        assert kb.get_task(conn, parked).status == "scheduled"
        _age_runs(conn, 1900)
        assert parked in wake_due_continuations(conn, now=int(time.time()) + 1900)
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert len(_spawned_ids(res)) == 1
        assert kb.get_task(conn, legacy).status == "blocked" or _spawned_ids(res) == [legacy]
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert _spawned_ids(res) == []
