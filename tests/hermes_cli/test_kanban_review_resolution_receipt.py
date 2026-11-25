"""Review decision evidence is per-call and never participates in routing."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_model_tier as tier, kanban_review_model as review
from hermes_cli import kanban_provider_walls as walls, kanban_llm_pacing_dispatch as pacing
from tests.hermes_cli import test_kanban_strong_tier as helpers

board = helpers.board


def routes(monkeypatch, active=None, holds=None, live=None):
    reads = []
    monkeypatch.setattr(review, "independent_routes", lambda *a, level="deep":
                        iter([("first", "anthropic"), ("second", "openai-codex")]) if level == "deep" else iter(()))
    monkeypatch.setattr(walls, "active_walls", lambda **kw: reads.append("active") or (active or {}))
    monkeypatch.setattr(pacing, "held_pools", lambda *a: reads.append("holds") or (holds or {}))
    monkeypatch.setattr(walls, "live_quota_wall", lambda p, m: reads.append((p, m)) or (live or {}).get(p))
    monkeypatch.setattr(review, "registry_refusal", lambda *a: None)
    return reads


def test_admission_records_actual_wall_and_final_route(board, monkeypatch):
    import time
    deadline = time.time() + 1000
    routes(monkeypatch, active={"anthropic": deadline})
    monkeypatch.setattr(tier, "_route_from_base", lambda c, t, base, **kw: base)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="default")
        kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)
        tier.apply_model_tier(conn, task, lane="review", cfg={})
        event = [e for e in kb.list_events(conn, tid) if e.kind == "model_tier"][-1]
        block = event.payload["review_resolution"]
        assert event.run_id == task.current_run_id
        assert block["version"] == 1
        assert block["outcome"] == "selected"
        assert block["initial_route"] == block["final_route"] == {
            "model": "second", "provider": "openai-codex", "reason": "review_unpinned"}
        assert block["downstream_changed"] is False
        first, second = block["candidates"]
        assert first["reasons"] == ["finite_wall_or_hold"]
        assert first["active_wall"] == {"state": "future", "deadline": deadline}
        assert second["live_quota_wall"] == {"state": "not_reported"}
        assert kb.get_task(conn, tid).model_override is None
        json.dumps(block, allow_nan=False)


@pytest.mark.parametrize("source,value,state,reason,picked", [
    ("active", None, "not_reported", None, "first"),
    ("active", 2000, "future", "finite_wall_or_hold", "second"),
    ("active", 900, "elapsed", None, "first"),
    ("active", True, "boolean_true", None, "first"),
    ("active", float("inf"), "unknown_deadline", None, "first"),
    ("active", float("nan"), "unknown_deadline", None, "first"),
    ("holds", 2000, "future", "finite_wall_or_hold", "second"),
    ("holds", 900, "elapsed", "pacing_hold_unknown_or_elapsed", "second"),
    ("holds", True, "boolean_true", "pacing_hold_unknown_or_elapsed", "second"),
    ("holds", float("inf"), "unknown_deadline", "pacing_hold_unknown_or_elapsed", "second"),
    ("live", None, "not_reported", None, "first"),
    ("live", False, "boolean_false", None, "first"),
    ("live", True, "boolean_true", "live_wall_unknown_deadline", "second"),
    ("live", 900, "elapsed", None, "first"),
    ("live", 2000, "future", "finite_wall_or_hold", "second"),
    ("live", float("nan"), "unknown_deadline", "live_wall_unknown_deadline", "second"),
])
def test_observations_preserve_routing_and_read_sequence(board, monkeypatch, source, value, state, reason, picked):
    import time
    reads = routes(monkeypatch, **{source: {"anthropic": value}})
    monkeypatch.setattr(time, "time", lambda: 1000)
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="review", assignee="default"))
        before = conn.total_changes
        plain = review.resolve_review_model(conn, task, {})
        expected_reads = reads[:]
        reads.clear()
        collector = {}
        enabled = review.resolve_review_model(conn, task, {}, collector=collector)
        assert enabled == plain
        assert enabled[0] == picked
        assert reads == expected_reads
        assert conn.total_changes == before
        assert collector["observed_at"] == 1000
        candidate = collector["candidates"][0]
        assert candidate[{"active": "active_wall", "holds": "pacing_hold", "live": "live_quota_wall"}[source]]["state"] == state
        assert candidate["reasons"] == ([reason] if reason else [])
        json.dumps(collector, allow_nan=False)


@pytest.mark.parametrize("unknown", [False, True])
def test_all_walls_refusal_is_in_memory_only(board, monkeypatch, unknown):
    import time
    reads = routes(monkeypatch, active={"anthropic": 2000, "openai-codex": 2500},
                   holds={"anthropic": True if unknown else 3000},
                   live={"openai-codex": True if unknown else 4000})
    monkeypatch.setattr(time, "time", lambda: 1000)
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="review", assignee="default"))
        errors = []
        for collector in (None, {}):
            reads.clear()
            with pytest.raises(review.ReviewModelUnavailable) as caught:
                review.resolve_review_model(conn, task, {}, collector=collector)
            errors.append((type(caught.value), str(caught.value), caught.value.__dict__, reads[:]))
            if collector is not None:
                assert collector["outcome"] == "unavailable"
                assert len(collector["candidates"]) == 2
        assert errors[0] == errors[1]
        assert isinstance(caught.value, review.ReviewRouteDeferred) is (not unknown)
        assert not [e for e in kb.list_events(conn, task.id) if e.kind == "model_tier"]


@pytest.mark.parametrize("identity", ["current", "missing", "stale", "foreign", "ended", "prior"])
def test_receipt_identity_never_redirects_admission(board, monkeypatch, identity):
    routes(monkeypatch)
    monkeypatch.setattr(tier, "_route_from_base", lambda c, t, base, **kw: base)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="default")
        kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)
        original = task.current_run_id
        if identity == "missing":
            task.current_run_id = None
        if identity == "stale":
            conn.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (tid,))
        if identity == "ended":
            conn.execute("UPDATE task_runs SET ended_at=1 WHERE id=?", (original,))
        if identity == "foreign":
            other = kb.create_task(conn, title="other", assignee="default")
            kb.claim_task(conn, other)
            task.current_run_id = kb.get_task(conn, other).current_run_id
            conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (task.current_run_id, tid))
        if identity == "prior":
            conn.execute("UPDATE task_runs SET ended_at=1 WHERE id=?", (original,))
            conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, current_run_id=NULL WHERE id=?", (tid,))
            conn.commit()
            kb.claim_task(conn, tid)
        conn.commit()
        assert tier.apply_model_tier(conn, task, lane="review", cfg={}) == "review_unpinned"
        event = [e for e in kb.list_events(conn, tid) if e.kind == "model_tier"][-1]
        assert event.run_id == task.current_run_id
        assert ("review_resolution" in event.payload) is (identity == "current")
        assert kb.get_task(conn, tid).model_override is None


@pytest.mark.parametrize("move", ["downstream", "pace"])
def test_final_route_follows_all_moves(board, monkeypatch, move):
    from hermes_cli import kanban_pace_reroute as pace
    routes(monkeypatch)
    monkeypatch.setattr(tier, "_route_from_base", lambda c, t, base, **kw:
                        ("later", "openai-codex", "review_unpinned+quota_reroute") if move == "downstream" else base)
    monkeypatch.setattr(pace, "take", lambda *a: ("later", "openai-codex", "anthropic") if move == "pace" else None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="default")
        kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)
        tier.apply_model_tier(conn, task, lane="review", cfg={})
        event = [e for e in kb.list_events(conn, tid) if e.kind == "model_tier"][-1]
        block = event.payload["review_resolution"]
        assert block["initial_route"]["model"] == "first"
        assert block["final_route"]["model"] == "later"
        assert block["final_route"]["reason"] == event.payload["reason"]
        assert block["downstream_changed"] is True


def test_registry_rejection_has_fixed_reason(board, monkeypatch):
    routes(monkeypatch)
    monkeypatch.setattr(review, "_override", lambda *a: ("class:deep", "auto", "review_override"))
    monkeypatch.setattr(tier, "class_unroutable_reason", lambda *a: None)
    monkeypatch.setattr(review, "registry_refusal", lambda model, *a: "private account label" if model == "first" else None)
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="review", assignee="default"))
        collector = {}
        assert review.resolve_review_model(conn, task, {}, collector=collector)[0] == "second"
        assert collector["candidates"][0]["reasons"] == ["registry_refused"]
        assert "private account label" not in json.dumps(collector)


@pytest.mark.parametrize("failure", ["observation", "identity_read", "event", "offer", "resolution"])
def test_fault_boundaries(board, monkeypatch, failure):
    import sqlite3
    from hermes_cli import kanban_pace_reroute as pace
    routes(monkeypatch)
    monkeypatch.setattr(tier, "_route_from_base", lambda c, t, base, **kw: base)
    def broken(*args, **kwargs):
        raise RuntimeError("diagnostic-only fault")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="default")
        kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)
        if failure == "observation":
            monkeypatch.setattr(review, "_observation", broken)
        if failure == "identity_read":
            conn.set_authorizer(lambda action, table, column, *a:
                                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and
                                table == "task_runs" and column == "ended_at" else sqlite3.SQLITE_OK)
        if failure == "event":
            original = kb._append_event
            def append(c, t, kind, *a, **kw):
                if kind == "model_tier":
                    raise sqlite3.OperationalError("receipt failure")
                return original(c, t, kind, *a, **kw)
            monkeypatch.setattr(kb, "_append_event", append)
        if failure == "offer":
            def lost(*a):
                raise pace.OfferLost("offer expired")
            monkeypatch.setattr(pace, "take", lost)
        if failure == "resolution":
            monkeypatch.setattr(review, "independent_routes", lambda *a, **kw: iter(()))
            from hermes_cli import kanban_model_assignment as assignment
            monkeypatch.setattr(assignment, "_assignee_config", lambda *a: {"model_routing": {"enabled": True}})
        if failure in ("observation", "identity_read"):
            assert tier.apply_model_tier(conn, task, lane="review", cfg={}) == "review_unpinned"
        else:
            expected = pace.OfferLost if failure == "offer" else review.ReviewModelUnavailable
            with pytest.raises(expected):
                tier.apply_model_tier(conn, task, lane="review", cfg={})
        conn.set_authorizer(None)
        events = [e for e in kb.list_events(conn, tid) if e.kind == "model_tier"]
        if failure in ("observation", "identity_read"):
            assert len(events) == 1
            assert "review_resolution" not in events[0].payload
        else:
            assert events == []


@pytest.mark.parametrize("kind", ["standard", "class", "legacy"])
def test_fallback_levels_and_legacy_none(board, monkeypatch, kind):
    import time
    from hermes_cli import kanban_model_assignment as assignment
    routes(monkeypatch, active={"anthropic": 2000})
    monkeypatch.setattr(time, "time", lambda: 1000)
    monkeypatch.setattr(review, "independent_routes", lambda *a, level="deep": iter(
        [] if kind == "legacy" else [("first", "anthropic")] if level == "deep" else [("second", "openai-codex")]))
    if kind == "legacy":
        monkeypatch.setattr(assignment, "_assignee_config", lambda *a: {})
    elif kind == "class":
        monkeypatch.setattr(review, "_override", lambda *a: ("class:deep", "auto", "review_override"))
        monkeypatch.setattr(tier, "class_unroutable_reason", lambda *a: None)
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="review", assignee="default"))
        collector = {}
        result = review.resolve_review_model(conn, task, {}, collector=collector)
        if kind == "legacy":
            assert result is None
            assert collector["outcome"] == "legacy_fallback"
            assert collector["candidates"] == []
        else:
            assert result == ("second", "openai-codex", "review_tier_fallback")
            assert [c["level"] for c in collector["candidates"]] == ["deep", "standard"]


def test_preview_dispatch_writes_nothing(board, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    from tests.hermes_cli.test_kanban_review_model import config
    config(board)
    routes(monkeypatch)
    monkeypatch.setattr(tier, "_route_from_base", lambda c, t, base, **kw: base)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="default")
        kb.claim_task(conn, tid)
        kb.request_review(conn, tid)
        before = list(conn.iterdump())
        seen = []
        dispatch.dispatch_once(conn, dry_run=True, spawn_fn=lambda *a: seen.append(a))
        assert list(conn.iterdump()) == before
        assert seen == []


@pytest.mark.parametrize("concurrent", [False, True])
def test_calls_have_no_shared_diagnostics(board, monkeypatch, concurrent):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    routes(monkeypatch)
    monkeypatch.setattr(review, "_override", lambda *a: None)
    monkeypatch.setattr(review, "independent_routes", lambda pin, cfg, assignee, **kw: iter([(assignee, "anthropic")]))
    def resolve(name):
        task = SimpleNamespace(assignee=name, model_override=None)
        collector = {}
        result = review.resolve_review_model(None, task, {}, collector=collector)
        assert not hasattr(task, "collector")
        return result, collector
    if concurrent:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(resolve, ["one", "two"]))
    else:
        results = [resolve(name) for name in ("one", "two")]
    assert [r[0][0] for r in results] == ["one", "two"]
    assert [[c["model"] for c in r[1]["candidates"]] for r in results] == [["one"], ["two"]]


def test_later_spawn_failure_keeps_historical_receipt(board, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    from tests.hermes_cli.test_kanban_review_model import config
    config(board)
    routes(monkeypatch)
    monkeypatch.setattr(tier, "_route_from_base", lambda c, t, base, **kw: base)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="default")
        kb.claim_task(conn, tid)
        kb.request_review(conn, tid)
        def spawn(*a):
            raise RuntimeError("later spawn failed")
        dispatch.dispatch_once(conn, dry_run=False, spawn_fn=spawn)
        events = kb.list_events(conn, tid)
        receipt = [e for e in events if e.kind == "model_tier"][-1]
        failure = [e for e in events if e.kind == "spawn_failed"][-1]
        assert receipt.run_id == failure.run_id
        assert receipt.payload["review_resolution"]["final_route"]["model"] == "first"
        assert kb.get_task(conn, tid).model_override is None


def test_admission_enabled_disabled_parity(board, monkeypatch):
    reads = routes(monkeypatch)
    monkeypatch.setattr(tier, "_route_from_base", lambda c, t, base, **kw: base)
    original = review._diagnose
    results = []
    with kbc.connect() as conn:
        for enabled in (False, True):
            tid = kb.create_task(conn, title="review", assignee="default")
            kb.claim_task(conn, tid)
            task = kb.get_task(conn, tid)
            monkeypatch.setattr(review, "_diagnose", original if enabled else lambda *a, **kw: None)
            reads.clear()
            result = tier.apply_model_tier(conn, task, lane="review", cfg={})
            payload = [e.payload for e in kb.list_events(conn, tid) if e.kind == "model_tier"][-1]
            assert ("review_resolution" in payload) is enabled
            payload.pop("review_resolution", None)
            results.append((result, payload, reads[:], task.model_override, task.provider_override,
                            kb.get_task(conn, tid).model_override))
    assert results[0] == results[1]


@pytest.mark.parametrize("handoff", [False, True])
def test_carbon_early_return_omits_unobserved_walls(board, monkeypatch, handoff):
    reads = routes(monkeypatch)
    monkeypatch.setattr(review, "_override", lambda *a: ("class:carbon", "auto", "review_override") if handoff else None)
    monkeypatch.setattr(tier, "class_unroutable_reason", lambda *a: None)
    monkeypatch.setattr(tier, "_route_from_base", lambda c, t, base, **kw: base)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="default")
        kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)
        task.model_override = "class:carbon"
        reason = tier.apply_model_tier(conn, task, lane="review", cfg={})
        assert reason == ("review_override" if handoff else "class")
        assert task.model_override == "class:carbon"
        event = [e for e in kb.list_events(conn, tid) if e.kind == "model_tier"][-1]
        assert "review_resolution" not in event.payload
        assert reads == []


def test_legacy_receipt_preserves_null_review_result(board, monkeypatch):
    from hermes_cli import kanban_model_assignment as assignment
    routes(monkeypatch)
    monkeypatch.setattr(review, "independent_routes", lambda *a, **kw: iter(()))
    monkeypatch.setattr(assignment, "_assignee_config", lambda *a: {})
    monkeypatch.setattr(tier, "class_unroutable_reason", lambda *a: None)
    monkeypatch.setattr(tier, "_route_from_base", lambda c, t, base, **kw: base)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="default")
        kb.claim_task(conn, tid)
        task = kb.get_task(conn, tid)
        tier.apply_model_tier(conn, task, lane="review", cfg={})
        event = [e for e in kb.list_events(conn, tid) if e.kind == "model_tier"][-1]
        block = event.payload["review_resolution"]
        assert block["outcome"] == "legacy_fallback"
        assert block["initial_route"] is None
        assert block["final_route"] == {"model": "class:deep", "provider": "auto", "reason": "review"}
        assert block["downstream_changed"] is True
        assert block["candidates"] == []
