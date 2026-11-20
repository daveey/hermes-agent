"""Temporary reviewer unavailability must not consume claim/failure budgets."""
import math

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from hermes_cli import kanban_review_model as review
from hermes_cli import kanban_llm_pacing_dispatch as pacing
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414

START = 1_800_000_000
RESET = START + 7200


@pytest.mark.parametrize("route_level", ["deep", "standard"])
@pytest.mark.parametrize("source", ["wall", "hold", "live", "overlap"])
@pytest.mark.parametrize("lane", ["review", "ready"])
def test_walled_review_not_claimed_and_unwalled_host_claims(
        board, monkeypatch, all_assignees_spawnable, source, lane, route_level):
    clock = [START]
    monkeypatch.setattr(kb.time, "time", lambda: clock[0])
    monkeypatch.setattr(tier, "load_tier_config", lambda: {"enabled": True})
    monkeypatch.setattr(review, "implementer_models", lambda *_a: {"gpt-6-astra", "gpt-6.1-sol"})
    monkeypatch.setattr(review, "independent_routes", lambda *_a, level="deep": iter([
        ("claude-fable-5-2", "anthropic"), ("claude-opus-5-5", "anthropic")] if level == route_level else []))
    monkeypatch.setattr(review, "registry_refusal", lambda *_a: None)
    blocked = [True]
    monkeypatch.setattr(walls, "active_walls", lambda **_kw: (
        {"anthropic": RESET} if blocked[0] and source in ("wall", "overlap") else {}))
    monkeypatch.setattr(pacing, "held_pools", lambda *_a: (
        {"anthropic": RESET - 60 if source == "overlap" else RESET}
        if blocked[0] and source in ("hold", "overlap") else {}))
    monkeypatch.setattr(walls, "live_quota_wall", lambda *_a: (
        RESET if blocked[0] and source == "live" else None))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Independent exact-head review" if lane == "ready" else "Build B",
                             assignee="a")
        if lane == "review":
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        seen = []
        for _ in range(review.DEFERRAL_LIMIT + 2):
            res = dispatch.dispatch_once(conn, spawn_fn=lambda task, _ws: seen.append(task.id))
            assert seen == []
            assert res.clock_held[tid] == ("review_provider_wall", RESET)
            assert res.respawn_guarded == [(tid, "review_provider_wall")]
            task = kb.get_task(conn, tid)
            assert task.status == lane and not task.claim_lock
            assert task.consecutive_failures == 0
            clock[0] += 301
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 0
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (tid,))]
        assert "claimed" not in kinds and "gave_up" not in kinds and "spawn_failed" not in kinds
        deferred = [kb._json_dict(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='review_route_deferred'", (tid,))]
        assert deferred == [{"reason": "review_provider_wall", "until": RESET}]
        # Same card on a host with an available independent route (no card mutation needed).
        blocked[0] = False
        dispatch.dispatch_once(conn, spawn_fn=lambda task, _ws: seen.append(task.id))
        assert seen == [tid]


@pytest.mark.parametrize("case", ["alternate", "standard", "expiry", "dry_run", "structural", "registry", "race", "earliest"])
def test_review_wait_boundaries(board, monkeypatch, case):
    from hermes_cli import kanban_review_admission as admission

    monkeypatch.setattr(kb.time, "time", lambda: START)
    monkeypatch.setattr(tier, "load_tier_config", lambda: {"enabled": True})
    monkeypatch.setattr(review, "implementer_models", lambda *_a: {"author-model"})
    routes = [("claude-opus-5-5", "anthropic")]
    if case in ("alternate", "earliest"):
        routes.append(("gpt-6.1-sol", "openai-codex"))
    if case == "structural":
        routes = []
    monkeypatch.setattr(review, "independent_routes", lambda *_a, level="deep": iter(
        routes if level == "deep" else [("gpt-6.1-sol", "openai-codex")] if case == "standard" else []))
    monkeypatch.setattr(review, "registry_refusal", lambda *_a: "refused" if case == "registry" else None)
    walled = {"anthropic": START - 1 if case == "expiry" else RESET}
    if case == "earliest":
        walled["openai-codex"] = RESET - 300
    monkeypatch.setattr(walls, "active_walls", lambda **_kw: walled)
    monkeypatch.setattr(walls, "live_quota_wall", lambda *_a: None)
    monkeypatch.setattr(pacing, "held_pools", lambda *_a: {})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Build", assignee="a")
        result = dispatch.DispatchResult()
        held = admission.hold(conn, tid, result, lane="review", dry_run=case == "dry_run")
        assert held == (case in ("dry_run", "race", "earliest"))
        events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                              (tid, admission.EVENT)).fetchall()
        assert bool(events) == (case in ("race", "earliest"))
        if case == "earliest":
            assert result.clock_held[tid] == (admission.REASON, RESET - 300)
        if case == "race":
            task = kb.get_task(conn, tid)
            with pytest.raises(review.ReviewRouteDeferred) as caught:
                tier.apply_model_tier(conn, task, lane="review", cfg={"enabled": True})
            monkeypatch.setattr(review, "deferral_exhausted", lambda *_a: True)
            assert review.admission_refusal(conn, tid, caught.value) == (True, False, None)


@pytest.mark.parametrize("deadline", [math.inf, math.nan, True, START - 1])
@pytest.mark.parametrize("wall", [None, RESET])
def test_unknown_or_elapsed_pacing_reset_is_not_a_deferral_deadline(
        board, monkeypatch, deadline, wall):
    monkeypatch.setattr(kb.time, "time", lambda: START)
    monkeypatch.setattr(review, "implementer_models", lambda *_a: {"author-model"})
    monkeypatch.setattr(review, "independent_routes", lambda *_a, **_kw: iter([
        ("claude-opus-5-5", "anthropic")]))
    monkeypatch.setattr(review, "registry_refusal", lambda *_a: None)
    monkeypatch.setattr(walls, "active_walls", lambda **_kw: {"anthropic": wall} if wall else {})
    monkeypatch.setattr(walls, "live_quota_wall", lambda *_a: None)
    monkeypatch.setattr(pacing, "held_pools", lambda *_a: {"anthropic": deadline})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Build", assignee="a")
        with pytest.raises(review.ReviewModelUnavailable) as caught:
            review.resolve_review_model(conn, kb.get_task(conn, tid), {})
        assert not isinstance(caught.value, review.ReviewRouteDeferred)


@pytest.mark.parametrize("failure", [ValueError("invalid review metadata"), RuntimeError("policy read failed")])
def test_preclaim_resolver_error_does_not_abort_dispatch(
        board, monkeypatch, all_assignees_spawnable, failure):
    monkeypatch.setattr(tier, "load_tier_config", lambda: {"enabled": True})
    monkeypatch.setattr(review, "resolve_review_model", lambda *_a: (_ for _ in ()).throw(failure))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Build", assignee="a")
        sibling = kb.create_task(conn, title="Ordinary implementation", assignee="a")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, _ws: seen.append(task.id))
        assert sibling in seen
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='claimed'",
                            (tid,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='review_route_deferred'",
                            (tid,)).fetchone()[0] == 0
