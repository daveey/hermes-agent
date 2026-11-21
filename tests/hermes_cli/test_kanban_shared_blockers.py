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
from typing import Optional

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_shared_blockers as ksb
import tools.kanban_tools as kt

BID = "github-actions-billing"


def _event_uid(ms, suffix):
    import uuid
    return str(uuid.UUID(int=(ms << 80) | (7 << 76) | (2 << 62) | suffix))


@pytest.mark.parametrize("own_only", [False, True])
@pytest.mark.parametrize("delta,expected", [(-1, True), (0, False), (1, False)])
@pytest.mark.parametrize("identity", ["v7", "missing", "v4"])
def test_operator_release_event_time(kanban_home, own_only, delta, expected, identity):
    """Remote insertion order is not occurrence order; ties retain local order."""
    with kbc.connect_closing() as conn:
        target = _blocked_behind(conn, "target")
        peer = _blocked_behind(conn, "peer")
        assert kb.unblock_task(conn, target)
        ms = int(time.time()) * 1000 + 500
        uid = lambda stamp, suffix: (_event_uid(stamp, suffix) if identity == "v7" else
                                    None if identity == "missing" else "00000000-0000-4000-8000-000000000001")
        # Unsynced fixture schema has no transport columns.
        conn.execute("ALTER TABLE task_events ADD COLUMN uid TEXT")
        # Keep the initial assertions older than the release, including their UUIDs.
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET uid=NULL, created_at=?", (ms // 1000 - 10,))
            conn.execute("UPDATE task_events SET uid=?, created_at=? WHERE task_id=? AND kind='unblocked'",
                         (uid(ms, 1), ms // 1000, target))
            kb._append_event(conn, target if own_only else peer, "scheduled", {"blocker_id": BID})
            conn.execute("UPDATE task_events SET uid=?, created_at=?, origin_gateway='remote' WHERE id=(SELECT MAX(id) FROM task_events)",
                         (uid(ms + delta, 2), ms // 1000 + (0 if identity == "v7" else delta)))
        assert ksb._unblocked_since_assertion(conn, target, BID, own_only=own_only) is expected
        assert ksb.shared_blocker_guard(conn, target, interval=3600) == (None if expected else ksb.GUARD_REASON)


@pytest.mark.parametrize("own_only", [False, True])
def test_release_selects_latest_clock_not_latest_local_id(kanban_home, own_only):
    with kbc.connect_closing() as conn:
        target = _blocked_behind(conn, "target")
        assert kb.unblock_task(conn, target)
        conn.execute("ALTER TABLE task_events ADD COLUMN uid TEXT")
        ms = int(time.time()) * 1000
        with kb.write_txn(conn):
            # All earlier assertions and the unblock share the same millisecond.
            # The UUID's random bits deliberately oppose local event order.
            conn.execute("UPDATE task_events SET uid=?, created_at=?", (_event_uid(ms, 999), ms // 1000))
            conn.execute("UPDATE task_events SET uid=? WHERE kind='unblocked'", (_event_uid(ms, 1),))
            kb._append_event(conn, target, "unblocked", {})
            conn.execute("UPDATE task_events SET uid=?, created_at=? WHERE id=(SELECT MAX(id) FROM task_events)",
                         (_event_uid(ms - 1000, 2), ms // 1000 - 1))
        assert ksb._unblocked_since_assertion(conn, target, BID, own_only=own_only)
        assert ksb.shared_blocker_guard(conn, target, interval=3600) is None


@pytest.mark.parametrize("own_only", [False, True])
@pytest.mark.parametrize("other_uid", ["v7", "missing"])
@pytest.mark.parametrize("backfilled,expected", [
    pytest.param("unblock", False, id="older-unblock-with-later-backfilled-uid-stays-held"),
    pytest.param("assertion", True, id="older-assertion-with-later-backfilled-uid-releases"),
])
def test_backfilled_uid_orders_by_created_at(kanban_home, own_only, other_uid, backfilled, expected):
    """Sync backfills uid-less rows with a v7 uid stamped at backfill time, not event
    time: a uid whose second disagrees with created_at must not outrank created_at."""
    with kbc.connect_closing() as conn:
        target = _blocked_behind(conn, "target")
        peer = _blocked_behind(conn, "peer")
        assert kb.unblock_task(conn, target)
        conn.execute("ALTER TABLE task_events ADD COLUMN uid TEXT")
        older = int(time.time()) - 100
        newer = older + 5
        backfill_ms = (older + 60) * 1000 + 250     # stamped long after both events
        consistent = lambda sec, suffix: _event_uid(sec * 1000 + 500, suffix) if other_uid == "v7" else None
        unblock_at = older if backfilled == "unblock" else newer
        assert_at = older if backfilled == "assertion" else newer
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET uid=NULL, created_at=?", (older - 10,))
            conn.execute("UPDATE task_events SET uid=?, created_at=? WHERE task_id=? AND kind='unblocked'",
                         (_event_uid(backfill_ms, 1) if backfilled == "unblock" else consistent(unblock_at, 1),
                          unblock_at, target))
            kb._append_event(conn, target if own_only else peer, "scheduled", {"blocker_id": BID})
            conn.execute("UPDATE task_events SET uid=?, created_at=? WHERE id=(SELECT MAX(id) FROM task_events)",
                         (_event_uid(backfill_ms, 2) if backfilled == "assertion" else consistent(assert_at, 2),
                          assert_at))
        assert ksb._unblocked_since_assertion(conn, target, BID, own_only=own_only) is expected
        assert ksb.shared_blocker_guard(conn, target, interval=3600) == (None if expected else ksb.GUARD_REASON)


@pytest.mark.parametrize("entry", ["cli", "tool"])
@pytest.mark.parametrize("running_peer", [False, True, "interval"])
def test_unblock_reports_shared_guard(kanban_home, monkeypatch, capsys, entry, running_peer):
    from argparse import Namespace
    from hermes_cli import kanban
    from tools.kanban_tools_self_heal import handle_unblock

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kbc.connect_closing() as conn:
        target = _blocked_behind(conn, "target")
        peer = _blocked_behind(conn, "peer")
        if running_peer is True:
            assert kb.unblock_task(conn, peer)
            assert kb.claim_task(conn, peer)
    if running_peer == "interval":
        # An assertion arriving between the unblock commit and its readback.
        original = kb.unblock_task

        def reassert(conn, tid, **kwargs):
            ok = original(conn, tid, **kwargs)
            with kb.write_txn(conn):
                kb._append_event(conn, peer, "scheduled", {"blocker_id": BID})
            return ok

        monkeypatch.setattr(kb, "unblock_task", reassert)
    if entry == "cli":
        assert kanban._cmd_unblock(Namespace(task_ids=[target], reason=None)) == 0
        result = capsys.readouterr().out
    else:
        result = handle_unblock({"task_id": target})
    with kbc.connect_closing() as conn:
        assert ksb.shared_blocker_guard(conn, target) == (ksb.GUARD_REASON if running_peer else None)
    expected = (f"still held by shared blocker {BID}: running sibling {peer}" if running_peer is True else
                "released: claimable on the next tick")
    if running_peer == "interval":
        from datetime import datetime
        from zoneinfo import ZoneInfo
        with kbc.connect_closing() as conn:
            state = ksb.blocker_states(conn)[BID]
            due = min(state.behind.values()) + ksb.probe_interval_seconds()
            at = datetime.fromtimestamp(due, ZoneInfo("America/Los_Angeles"))
            expected = f"still held by shared blocker {BID}: probe interval due at {at:%Y-%m-%d %H:%M:%S} PT"
            writes = conn.total_changes
            assert ksb.unblock_verdict(conn, target) == expected
            assert conn.total_changes == writes
    assert expected in result


DEGRADED = "shared-blocker verdict unavailable: RuntimeError"


def _broken_evaluator(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("evaluator broke")

    monkeypatch.setattr(ksb, "_shared_blocker_hold", boom)


def test_verdict_failure_keeps_committed_tool_unblock(kanban_home, monkeypatch):
    """The verdict runs after the unblock committed: its failure must not report the unblock as failed."""
    from tools.kanban_tools_self_heal import handle_unblock

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kbc.connect_closing() as conn:
        target = _blocked_behind(conn, "target")
    _broken_evaluator(monkeypatch)
    result = json.loads(handle_unblock({"task_id": target}))
    assert result.get("ok") is True, result
    assert result["shared_blocker_verdict"] == DEGRADED
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, target).status == "ready"


def test_verdict_failure_keeps_bulk_cli_unblock_going(kanban_home, monkeypatch, capsys):
    """One failing verdict must not abort the remaining ids of ``hermes kanban unblock a b c``."""
    from argparse import Namespace
    from hermes_cli import kanban

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kbc.connect_closing() as conn:
        ids = [_blocked_behind(conn, f"card {n}") for n in range(3)]
    _broken_evaluator(monkeypatch)
    assert kanban._cmd_unblock(Namespace(task_ids=ids, reason=None)) == 0
    out = capsys.readouterr().out
    with kbc.connect_closing() as conn:
        assert [kb.get_task(conn, tid).status for tid in ids] == ["ready"] * 3
    for tid in ids:
        assert f"Unblocked {tid}; {DEGRADED}" in out


@pytest.mark.parametrize("own_only, indexed, index", [
    (False, True, "idx_events_blocker_assert (created_at>?)"),
    (False, False, "idx_events_kind_task_id (kind=?)"),   # a board this core has not migrated
    (True, True, "(task_id=?"),
])
def test_assertion_scan_reads_only_the_unblock_window(kanban_home, own_only, indexed, index):
    """The guard runs per held card per tick: its assertion read must be an index range from the
    unblock's second, not a walk of every blocked/scheduled event on the board (PR #2946 review)."""
    with kbc.connect_closing() as conn:
        target = _blocked_behind(conn, "target")
        assert kb.unblock_task(conn, target)
        if not indexed:
            with kb.write_txn(conn):
                conn.execute("DROP INDEX idx_events_blocker_assert")
        with kb.write_txn(conn):   # a newer assertion in the window still decides
            kb._append_event(conn, target, "scheduled", {"blocker_id": BID})
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        try:
            assert ksb._unblocked_since_assertion(conn, target, BID, own_only=own_only) is False
        finally:
            conn.set_trace_callback(None)
        scans = [s for s in statements if "blocker_id" in s]
        assert len(scans) == 1, statements
        plan = " ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + scans[0]))
        assert index in plan and "SCAN" not in plan, plan
        assert own_only is False or "idx_events_blocker_assert" not in plan, plan


def test_drifted_task_events_rebuild_keeps_the_assertion_index(kanban_home):
    """DROP TABLE in the drift rebuild takes every index down; the rebuild must restore this one too."""
    import sqlite3
    from hermes_cli import kanban_db_connect as kdc

    db_path = kanban_home / "legacy-events.db"
    seed = sqlite3.connect(db_path)
    seed.execute("CREATE TABLE task_events (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,"
                 " payload TEXT, created_at INTEGER NOT NULL)")
    seed.execute("INSERT INTO task_events VALUES ('e1', 't1', 'scheduled', '{\"blocker_id\": \"x\"}', 5)")
    seed.commit()
    seed.close()
    conn = kdc.connect(db_path)
    try:
        assert not kdc._table_has_drifted(conn, "task_events")
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                            "AND name='idx_events_blocker_assert'").fetchone() is not None
    finally:
        conn.close()


def _enabled_routes():
    route = {"billing": "subscription", "weight": 1, "eligible": True, "funding_verified": True,
             "capabilities": {"tools": True, "streaming": True}}
    return {"model_routing": {"enabled": True, "routes": {
        level: [dict(route, provider="anthropic", model="claude-opus-4"),
                dict(route, provider="openai-codex", model="gpt-5")] for level in ("fast", "standard", "deep")}}}


_ENABLED_ROUTES = _enabled_routes()


@pytest.fixture
def scoped_routing(monkeypatch):
    from hermes_cli import kanban_model_tier as tier
    from hermes_cli import kanban_model_assignment as assignment
    monkeypatch.setattr(tier, "load_tier_config", lambda: {
        "default_model": "class:standard", "strong_model": "class:deep"})
    monkeypatch.setattr(assignment, "_assignee_config", lambda _: _ENABLED_ROUTES)
    # The host-level routability probe reads the same isolated enabled class routes.
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda *a, **k: _ENABLED_ROUTES)


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



@pytest.mark.parametrize("park", ["continue", "schedule"])
@pytest.mark.parametrize("archived", [False, True], ids=["done", "archived"])
def test_finished_other_route_wall_has_no_live_owner(kanban_home, scoped_routing, monkeypatch, park, archived):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="implementation", assignee="default")
        assert kb.claim_task(conn, tid)
        _route_run(conn, tid, "openai-codex")
        run_id = kb._current_run_id(conn, tid)
        kbd._set_worker_pid(conn, tid, os.getpid())
        viewer = kb.create_task(conn, title="review", assignee="default")
        assert kb.claim_task(conn, viewer)
        _route_run(conn, viewer, "anthropic")
    with monkeypatch.context() as worker:
        worker.setenv("HERMES_KANBAN_TASK", tid)
        worker.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
        args = {"blocker_id": BID, "blocker_provider": "anthropic"}
        if park == "continue":
            result = kt._handle_continue({**args, "checkpoint": "provider wall", "resume_after_seconds": 600})
        else:
            result = kt._handle_schedule({**args, "reason": f"SCHEDULED_UNTIL={_future_iso(600)} provider wall"})
        assert json.loads(result)["status"] == "scheduled", result
    with kbc.connect_closing() as conn:
        assert ksb.blocker_states(conn)[BID].is_open
        assert BID in ksb.render_blocker_context(conn, viewer)
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid)
        _route_run(conn, tid, "openai-codex")
        assert kb.complete_task(conn, tid, result="implementation done")
        if archived:
            assert kb.archive_task(conn, tid)
        st = ksb.blocker_states(conn)[BID]
        assert not st.behind and st.cleared_by is None
        assert not st.is_open
        assert BID not in ksb.render_blocker_context(conn, viewer)


@pytest.mark.parametrize("outcome", ["completed", "review_requested", "changes_requested"])
def test_terminal_outcome_blocker_metadata_is_not_assertion(kanban_home, outcome):
    assert ksb.run_effect(outcome, {"blocker_id": BID}) == ("clear", None)
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "implementation")
        _scope(conn, tid)
        _age_runs(conn, 60)
        before = ksb.blocker_states(conn)[BID]
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid)
        _route_run(conn, tid, "anthropic")
        with kb.write_txn(conn):
            kb._end_run(conn, tid, outcome=outcome, summary="finished work",
                        metadata={"blocker_id": BID})
            conn.execute("UPDATE tasks SET status=? WHERE id=?",
                         ("done" if outcome == "completed" else "review", tid))
        after = ksb.blocker_states(conn)[BID]
        assert (after.asserted_at, after.reason, after.blocker_provider) == (
            before.asserted_at, before.reason, "anthropic")
        assert after.cleared_by is not None and not after.is_open


@pytest.mark.parametrize("status", ["blocked", "scheduled", "ready", "running"])
def test_live_owner_wall_stays_open_and_renders(kanban_home, scoped_routing, status):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "live owner")
        _scope(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        viewer = kb.create_task(conn, title="review", assignee="default")
        assert kb.claim_task(conn, viewer)
        _route_run(conn, viewer, "anthropic")  # historical concrete receipt (neutral admission records class/auto)
        st = ksb.blocker_states(conn)[BID]
        assert st.is_open and set(st.behind) == {tid}
        assert BID in ksb.render_blocker_context(conn, viewer)
        assert "applicable to current route" in ksb.render_blocker_context(conn, viewer)


@pytest.mark.parametrize("blocker_provider", ["openai-codex", "anthropic"])
def test_pinned_wall_preview_matches_dispatch(kanban_home, monkeypatch, tmp_path, blocker_provider):
    from hermes_cli import kanban_model_tier as tier, kanban_provider_walls as walls

    cfg = {"default_model": "class:standard", "strong_model": "class:deep"}
    monkeypatch.setattr(tier, "load_tier_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda *a, **k: _ENABLED_ROUTES)
    monkeypatch.setattr(walls, "walls_path", lambda: tmp_path / "walls.json")
    walls.record_wall("openai-codex", 3600, path=tmp_path / "walls.json")
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "pinned")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=?", (json.dumps({
                "blocker_id": BID, "blocker_provider": blocker_provider,
            }), tid))
            conn.execute("UPDATE tasks SET model_override=?, provider_override=? WHERE id=?",
                         ("gpt-6-astra", "openai-codex", tid))
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        preview = ksb.task_route(conn, tid)
        guard = ksb.shared_blocker_guard(conn, tid, interval=1800)
        task = kb.get_task(conn, tid)
        assert task is not None
        tier.apply_model_tier(conn, task, lane="ready", cfg=cfg)
        assert preview[:2] == tier.effective_route(task)[:2]
        # Known Codex wall chooses an eligible Anthropic peer dynamically; the
        # provider-scoped blocker follows that actual route, not the retired pin.
        assert preview == ("claude-sonnet-5-5", "anthropic", "ready")
        assert guard == (ksb.GUARD_REASON if blocker_provider == 'anthropic' else None)
        row = kb.get_task(conn, tid)
        assert (row.model_override, row.provider_override) == ("gpt-6-astra", "openai-codex")



def test_scoped_next_lane_and_context(kanban_home, scoped_routing):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "scoped")
        _scope(conn, tid)
        st = ksb.blocker_states(conn)[BID]
        assert (st.blocker_provider, st.blocker_model, st.blocker_lane) == (
            "anthropic", "claude-sonnet-4", "review")
        # Neutral admission leaves the next route provider-unknown (class): an anthropic-scoped wall holds it
        # conservatively in every lane, and the context names the class and lane, never a guessed provider.
        assert ksb.shared_blocker_guard(conn, tid, interval=1800) == ksb.GUARD_REASON
        ctx = ksb.render_blocker_context(conn, tid)
        assert "model=class:standard" in ctx and "lane=ready" in ctx and "provider=openai-codex" not in ctx
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        assert ksb.shared_blocker_guard(conn, tid, interval=1800) == ksb.GUARD_REASON
        ctx = ksb.render_blocker_context(conn, tid)
        # The review lane resolves concretely from the isolated enabled class routes: this run is the probe.
        assert "lane=review" in ctx and "This run is the probe" in ctx


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
        # Resume lane survives even though unknown authors cannot be allocated a reviewer.
        assert ksb.task_route(conn, tid) == (None, None, "review")
        from hermes_cli import kanban_review_model as review
        with pytest.raises(review.ReviewModelUnavailable, match='author is unknown'):
            review.resolve_review_model(conn, kb.get_task(conn, tid), {})
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
            conn.execute("UPDATE tasks SET status='review', model_override='claude-sonnet-4', "
                         "provider_override='anthropic' WHERE id=?", (tid,))
        st = ksb.blocker_states(conn)[BID]
        assert st.is_open and not st.probe_starts
        # The review lane's route comes from the isolated enabled class routes, never the row's legacy pin.
        assert ksb.task_route(conn, tid) == ("claude-opus-4", "anthropic", "review")


def test_profile_default_when_tiering_off(kanban_home, scoped_routing, monkeypatch):
    from hermes_cli import kanban_model_tier as tier
    monkeypatch.setattr(tier, "load_tier_config", dict)
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "profile")
        _scope(conn, tid)
        assert ksb.task_route(conn, tid) == ("class:standard", None, "ready")  # a profile model is never the route
        assert ksb.shared_blocker_guard(conn, tid) == ksb.GUARD_REASON


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
        assert ksb.task_route(conn, tid) == ("class:deep", None, "ready")
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == count
        task = kb.get_task(conn, tid)
        assert task is not None and task.model_override is None


def test_profile_class_provider_remains_unknown(kanban_home, scoped_routing, monkeypatch):
    from hermes_cli import kanban_model_tier as tier, kanban_model_assignment as assignment
    monkeypatch.setattr(tier, "load_tier_config", dict)
    monkeypatch.setattr(assignment, "_assignee_config", lambda _: {
        "model": {"default": "class:standard", "provider": "openai-codex"}, **_ENABLED_ROUTES})
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "class")
        _scope(conn, tid)
        assert ksb.task_route(conn, tid) == ("class:standard", None, "ready")
        assert ksb.shared_blocker_guard(conn, tid) == ksb.GUARD_REASON


def test_unknown_historical_route_neither_probes_nor_clears(kanban_home):
    """No receipt and no resolvable route: under a scoped wall the run is no evidence, while
    the other card's unknown next route is still held (conservative)."""
    with kbc.connect_closing() as conn:
        a, b = _blocked_behind(conn, "a"), _blocked_behind(conn, "b")
        _scope(conn, a)
        _scope(conn, b)
        kb.unblock_task(conn, a)
        kb.claim_task(conn, a)
        assert ksb.task_route(conn, a)[1] is None
        assert a not in ksb.blocker_states(conn)[BID].probe_starts
        kb.complete_task(conn, a, result="done on an unknown route")
        st = ksb.blocker_states(conn)[BID]
        assert st.is_open and st.cleared_by is None and set(st.behind) == {b}
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (b,))
        assert ksb.shared_blocker_guard(conn, b, interval=1800) == ksb.GUARD_REASON


def test_unscoped_wall_still_holds_other_provider(kanban_home, scoped_routing):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "legacy")
        assert ksb.shared_blocker_guard(conn, tid, interval=1800) == ksb.GUARD_REASON


# --- Real admission receipts (apply_model_tier via dispatch_once), no handwritten routes. ---

WALL = "anthropic-usage-limit"


def _write_home_cfg(home: Path, *, profile_model: str, profile_provider: str = "", kanban: str = "") -> None:
    lines = ["model:", f"  default: {profile_model}"]
    if profile_provider:
        lines.append(f"  provider: {profile_provider}")
    if kanban:
        lines += ["kanban:", *(f"  {line}" for line in kanban.splitlines())]
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    home.joinpath("config.yaml").write_text("\n".join(lines) + "\n" + _managed_yaml(), encoding="utf-8")


def _park_behind(conn, tid: str, provider: Optional[str] = "anthropic") -> None:
    """The worker's own park: kanban_continue(blocker_id, blocker_provider), back to ready at once."""
    from hermes_cli.kanban_continuation import continue_task
    meta = {"blocker_id": WALL, **({"blocker_provider": provider} if provider else {})}
    res = continue_task(conn, tid, checkpoint="Anthropic usage limit reached for the review step",
                        resume_after_seconds=0, metadata=meta)
    assert res["ok"], res
    assert kb.get_task(conn, tid).status == "ready"


def _receipt(conn, tid):
    from hermes_cli.kanban_model_tier import RUN_ROUTE_EVENT
    run_id = kb.list_runs(conn, tid)[-1].id
    rows = [e.payload for e in kb.list_events(conn, tid) if e.kind == RUN_ROUTE_EVENT and e.run_id == run_id]
    assert rows, f"no admission receipt for {tid} run {run_id}"
    return rows[-1]


@pytest.mark.parametrize("seeded, clears", [
    (None, False),                    # real neutral admission: class receipt, provider unknown, not evidence
    ("openai-codex", False),          # historical concrete Codex receipt: not evidence for an Anthropic wall
    ("anthropic", True),              # control: a historical Anthropic receipt clears
])
def test_admitted_route_decides_clear_evidence(kanban_home, spawns, monkeypatch, seeded, clears):
    """A card's admission receipt completing must not clear an Anthropic-scoped wall unless that receipt
    names Anthropic. Neutral admission records a provider-unknown class; concrete receipts are historical
    seeds written into the disposable DB."""
    _write_home_cfg(kanban_home, profile_model="class:standard")
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda *a, **k: _ENABLED_ROUTES)
    _spawned, fake_spawn = spawns
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="probe card", assignee="default")
        assert kb.claim_task(conn, tid)
        _park_behind(conn, tid)
        _age_runs(conn, 1900)   # past the probe interval: the card may spawn whatever its route
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn)
        assert _spawned_ids(res) == [tid]
        receipt = _receipt(conn, tid)
        assert receipt["model"] == "class:standard" and receipt["source"] == "class" and receipt["provider"] is None
        if seeded:
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "run_route", {"model": "m", "provider": seeded, "lane": "ready",
                                                          "source": "pin", "tier_reason": "legacy_receipt"},
                                 run_id=kb._current_run_id(conn, tid))
        st = ksb.blocker_states(conn)[WALL]
        assert (tid in st.probe_starts) is clears
        assert kb.complete_task(conn, tid, result="done")
        st = ksb.blocker_states(conn)[WALL]
        assert (st.cleared_by is not None) is clears
        assert not st.is_open  # no live card remains, independently of clear evidence


@pytest.mark.parametrize("order", ["unscoped_first", "scoped_first"])
def test_scoped_assertion_never_narrows_unscoped_same_id(kanban_home, order):
    with kbc.connect_closing() as conn:
        a = kb.create_task(conn, title="a", assignee="default")
        b = kb.create_task(conn, title="b", assignee="default")
        first, second = (None, "anthropic") if order == "unscoped_first" else ("anthropic", None)
        for tid, provider in ((a, first), (b, second)):
            assert kb.claim_task(conn, tid)
            _park_behind(conn, tid, provider)
        st = ksb.blocker_states(conn)[WALL]
        assert st.blocker_provider is None
        # Board-wide: a card known to run on Codex is held too.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET model_override='gpt-5', provider_override='openai-codex' WHERE id=?", (a,))
        assert ksb.shared_blocker_guard(conn, a, interval=1800) == ksb.GUARD_REASON


@pytest.mark.parametrize("providers, expected", [
    (("anthropic", "openai-codex"), None),       # differing providers: board-wide
    (("anthropic", "anthropic"), "anthropic"),   # agreeing providers keep the scope
])
def test_assertion_scope_union(kanban_home, providers, expected):
    with kbc.connect_closing() as conn:
        for i, provider in enumerate(providers):
            tid = kb.create_task(conn, title=f"c{i}", assignee="default")
            assert kb.claim_task(conn, tid)
            _park_behind(conn, tid, provider)
        assert ksb.blocker_states(conn)[WALL].blocker_provider == expected


def test_dispatch_codex_runs_behind_anthropic_wall_and_opus_probes_after_interval(kanban_home, spawns, monkeypatch):
    """Dispatch consumes real allocator decisions, never profile-model guesses or stored pins.

    This composes the allocator and dispatcher at the tier-resolution seam; credential/provider
    calls remain outside this isolated test. Unknown allocation is held conservatively first.
    """
    from agent.model_routing import resolve_model_route
    from agent.model_routing_state import RoutingState
    from hermes_cli import kanban_model_tier as tier

    _write_home_cfg(kanban_home, profile_model="class:standard")
    state_path = kanban_home / "provider-proof.sqlite3"
    allocated = {}
    # Exercise real subscription/capability filtering and durable weighted allocation. Keep both
    # providers eligible: discover the resulting routes rather than request a concrete model.
    for index in range(6):
        sid = f"provider-proof-{index}"
        route = resolve_model_route("class:standard", "auto", config=_ENABLED_ROUTES,
                                    session_id=sid, requirements={"tools": True, "streaming": True},
                                    state_path=state_path)
        metadata = RoutingState(state_path).peek(sid, "standard")
        assert metadata is not None
        assert metadata == route["metadata"] and not metadata.get("exact_pin")
        allocated.setdefault(route["provider"], []).append(route)
    assert len(allocated["openai-codex"]) >= 2 and allocated["anthropic"]
    decisions = {}
    original_resolve = tier.resolve_dispatch_tier

    def allocated_tier(conn, task, *, lane, cfg=None):
        base = original_resolve(conn, task, lane=lane, cfg=cfg)
        if task.id not in decisions:
            return base
        route = decisions[task.id]
        return route["model"], route["provider"], "class+allocated"

    seen: dict[str, tuple] = {}

    def spawn(task, workspace, board=None):
        seen[task.id] = (task.model_override, task.provider_override)

    with kbc.connect_closing() as conn:
        opus = kb.create_task(conn, title="adversarial review", assignee="default")
        codex1 = kb.create_task(conn, title="impl one", assignee="default")
        codex2 = kb.create_task(conn, title="impl two", assignee="default", priority=5)
        for tid in (opus, codex1, codex2):
            assert kb.claim_task(conn, tid)
            _park_behind(conn, tid)

        # Before allocation establishes a concrete provider, all three routes are unknown:
        # no claims, no new runs and no provider launch are permitted within the interval.
        runs_before = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
        unknown = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert _spawned_ids(unknown) == [] and seen == {}
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == runs_before
        assert {tid for tid, why in unknown.respawn_guarded if why == ksb.GUARD_REASON} == {opus, codex1, codex2}
        decisions.update({opus: allocated["anthropic"][0], codex1: allocated["openai-codex"][0],
                          codex2: allocated["openai-codex"][1]})
        monkeypatch.setattr(tier, "resolve_dispatch_tier", allocated_tier)

        # Within the interval: both dynamically allocated Codex cards spawn, Anthropic is held.
        res = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert sorted(_spawned_ids(res)) == sorted([codex1, codex2])
        assert (opus, ksb.GUARD_REASON) in res.respawn_guarded
        for tid in (codex1, codex2):
            assert _receipt(conn, tid)["provider"] == "openai-codex"
        assert not any(e.kind == "unblocked" for t in (opus, codex1, codex2) for e in kb.list_events(conn, t))

        # A Codex completion is no evidence about the Anthropic wall.
        assert kb.complete_task(conn, codex1, result="impl done")
        st = ksb.blocker_states(conn)[WALL]
        assert st.is_open and st.cleared_by is None and not st.probe_starts
        res = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert opus not in _spawned_ids(res) and (opus, ksb.GUARD_REASON) in res.respawn_guarded

        # Interval passes. codex2 checkpoints (no blocker id: still behind the wall) and starts a
        # FRESH Codex run first (higher priority, one slot), so its start is newer than the interval.
        _age_runs(conn, 1900)
        res = continue_task_ok(conn, codex2)
        res = kbd.dispatch_once(conn, spawn_fn=spawn, max_spawn=1)
        assert _spawned_ids(res) == [codex2]
        st = ksb.blocker_states(conn)[WALL]
        assert codex2 in st.behind and codex2 not in st.probe_starts

        # The running Codex card is neither "a probe in flight" nor a recent probe start.
        res = kbd.dispatch_once(conn, spawn_fn=spawn)
        assert _spawned_ids(res) == [opus]
        assert seen[opus] == ("claude-opus-4", "anthropic")
        assert _receipt(conn, opus)["provider"] == "anthropic"
        st = ksb.blocker_states(conn)[WALL]
        assert st.is_open and opus in st.probe_starts
        assert not any(e.kind == "unblocked" for e in kb.list_events(conn, opus))
        # A live matching-provider probe owns custody: no duplicate launch on another tick.
        assert _spawned_ids(kbd.dispatch_once(conn, spawn_fn=spawn)) == []
        for tid in (opus, codex1, codex2):
            stored = kb.get_task(conn, tid)
            assert stored is not None
            assert stored.model_override is None and stored.provider_override is None
            assert not stored.model_pin_mandatory


def continue_task_ok(conn, tid):
    from hermes_cli.kanban_continuation import continue_task
    res = continue_task(conn, tid, checkpoint="impl checkpoint", resume_after_seconds=0)
    assert res["ok"], res
    return res


def test_reassertion_guidance_carries_provider(kanban_home, scoped_routing):
    """Context and the blocker_id tool description both tell a reassertion to pass blocker_provider,
    since an unscoped reassertion widens the id to board-wide (union rule)."""
    from tools.kanban_tools_schemas import _BLOCKER_ID_DESC
    assert "blocker_provider" in _BLOCKER_ID_DESC and "board-wide" in _BLOCKER_ID_DESC
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "scoped")
        _scope(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        ctx = ksb.render_blocker_context(conn, tid)
        assert "This run is the probe" in ctx
        assert f'end again with `blocker_id: "{BID}"` and `blocker_provider: "anthropic"`' in ctx
        assert "pass the same `blocker_provider`" in ctx and "board-wide" in ctx
        # Control: an unscoped probe is told only the id.
        other = _blocked_behind(conn, "unscoped", blocker_id="other-wall")
        ctx = ksb.render_blocker_context(conn, other)
        assert 'end again with `blocker_id: "other-wall"`. ' in ctx


@pytest.mark.parametrize("scoped", [False, True], ids=["unscoped", "scoped"])
def test_untyped_classification_clear_blocker_synthesizes_clearing_run(kanban_home, scoped):
    """A breaker-parked UNTYPED card (no live run) classified in place with clear_blocker=True
    gets a synthesized closed run carrying the flag; that run clears the blocker it was behind."""
    with kbc.connect_closing() as conn:
        a, b = _blocked_behind(conn, "a"), _blocked_behind(conn, "b")
        if scoped:
            _scope(conn, a)
            _scope(conn, b)
        assert ksb.blocker_states(conn)[BID].is_open
        with kb.write_txn(conn):   # the failure breaker's shape: blocked, untyped, no live run
            conn.execute("UPDATE tasks SET status='blocked', block_kind=NULL, current_run_id=NULL "
                         "WHERE id=?", (a,))
        runs_before = len(kb.list_runs(conn, a))
        assert kb.block_task(conn, a, kind="capability", reason="needs a different tool; wall gone",
                             clear_blocker=True)
        runs = kb.list_runs(conn, a)
        assert len(runs) == runs_before + 1
        synth = runs[-1]
        assert synth.metadata == {ksb.CLEAR_FLAG: True} and synth.outcome == "blocked"
        ev = [e for e in kb.list_events(conn, a) if e.kind == "blocked"][-1]
        assert ev.payload["classified_in_place"] is True and ev.run_id == synth.id
        st = ksb.blocker_states(conn)[BID]
        assert (st.is_open, st.cleared_by) == (False, (a, synth.id)), (scoped, st)


def test_explicit_clear_from_other_provider_run_clears_scoped_wall(kanban_home, scoped_routing):
    """Control for the route-independent explicit clear: a Codex run's completion would not clear
    the Anthropic-scoped wall, but its park with clear_blocker=True does."""
    with kbc.connect_closing() as conn:
        a, b = _blocked_behind(conn, "a"), _blocked_behind(conn, "b")
        _scope(conn, a)
        _scope(conn, b)
        kb.unblock_task(conn, a)
        assert kb.claim_task(conn, a)
        _route_run(conn, a, "openai-codex")
        assert kb.block_task(conn, a, kind="capability", reason="other reason; wall seen gone",
                             clear_blocker=True)
        st = ksb.blocker_states(conn)[BID]
        assert not st.is_open and st.cleared_by[0] == a



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
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    # Lifecycle dispatch uses the real model-neutral class eligibility boundary.
    (home / 'config.yaml').write_text(_managed_yaml())
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
