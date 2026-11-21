"""Pinned implementation cards still get a registry-eligible review route; author model equality is not a gate."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_model_tier as tier
from tests.hermes_cli import test_kanban_strong_tier as helpers

board = helpers.board
SONNET, OPUS, FABLE = helpers.SONNET, helpers.OPUS, "claude-fable-5-2"


def config(board, *, strong=OPUS, routes=None, enabled=True):
    route_models = [OPUS, FABLE] if routes is None else routes
    cfg = {"kanban": {"default_model": SONNET, "strong_model": strong},
           "model_routing": {"enabled": enabled, "routes": {"deep": [
               {"model": model, "provider": "anthropic", "weight": 1,
                "billing": "subscription", "eligible": True, "funding_verified": True,
                "capabilities": {"tools": True, "streaming": True}} for model in route_models]}}}
    board.joinpath("config.yaml").write_text(json.dumps(cfg), encoding="utf-8")
    return cfg["kanban"]


def task(conn, model=SONNET):
    return kb.get_task(conn, kb.create_task(conn, title="review fixture", assignee="default",
                                          model_override=model, provider_override="anthropic"))


@pytest.mark.parametrize("lane,skills", [("review", []), ("ready", ["sdlc-review"])])
def test_pinned_review_uses_strong(board, lane, skills):
    cfg = config(board)
    with kbc.connect() as conn:
        t = task(conn)
        t.skills = skills
        assert tier.resolve_model_tier(conn, t, lane=lane, cfg=cfg) == (OPUS, "anthropic", "review_unpinned")


def test_strong_pin_may_review_on_the_same_strong_route(board):
    cfg = config(board)
    with kbc.connect() as conn:
        assert tier.resolve_model_tier(conn, task(conn, OPUS), lane="review", cfg=cfg) == (
            OPUS, "anthropic", "review_unpinned")


@pytest.mark.parametrize("failed", [False, True])
def test_ready_and_retry_preserve_pin(board, failed):
    cfg = config(board)
    with kbc.connect() as conn:
        t = task(conn)
        if failed:
            helpers._fail_once(conn, t.id)
            t = kb.get_task(conn, t.id)
        assert tier.resolve_model_tier(conn, t, lane="ready", cfg=cfg) == (None, None, "pinned")


def test_review_handoff_override_wins(board):
    cfg = config(board)
    with kbc.connect() as conn:
        t = task(conn)
        kb.claim_task(conn, t.id)
        assert kb.request_review(conn, t.id, metadata={"review_model": FABLE})
        t = kb.get_task(conn, t.id)
        assert tier.resolve_model_tier(conn, t, lane="review", cfg=cfg) == (
            FABLE, "anthropic", "review_override")


def test_dispatch_receipt_and_owner_use_review_model_without_changing_row(board):
    config(board)
    seen = {}
    with kbc.connect() as conn:
        t = task(conn)
        kb.claim_task(conn, t.id)
        kb.request_review(conn, t.id, summary="fixture ready")
        dispatch.dispatch_once(conn, dry_run=False,
                               spawn_fn=lambda item, ws: seen.update(model=item.model_override))
        stored = kb.get_task(conn, t.id)
        events = [e.payload for e in kb.list_events(conn, t.id) if e.kind == "model_tier"]
        assert seen["model"] == OPUS
        assert stored.model_override == SONNET
        assert tier.owner_model_pin(conn, stored) == (OPUS, "anthropic")
        assert events[-1] == {"model": OPUS, "provider": "anthropic", "reason": "review_unpinned",
                              "lane": "review", "review_independence": "separate_execution"}


@pytest.mark.parametrize("failure", ["no_route", "receipt_write", "bad_metadata", "class_pin"])
def test_review_admission_failure_never_spawns_or_spends_retry(board, monkeypatch, failure):
    import sqlite3
    # Enabled but empty routing is the refusal, not the author's model or legacy profile allocation.
    config(board, strong="", routes=[])
    seen = []
    with kbc.connect() as conn:
        t = task(conn, OPUS)
        kb.claim_task(conn, t.id)
        kb.request_review(conn, t.id)
        if failure == "bad_metadata":
            conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=?", (
                json.dumps({"review_model": []}), t.id))
            conn.commit()
        if failure == "receipt_write":
            config(board, strong=FABLE, routes=[FABLE])
            original = kb._append_event

            def append(conn, task_id, kind, *args, **kwargs):
                if kind == "model_tier":
                    raise sqlite3.OperationalError("fixture write failure")
                return original(conn, task_id, kind, *args, **kwargs)

            monkeypatch.setattr(kb, "_append_event", append)
        if failure == "class_pin":
            conn.execute("UPDATE tasks SET model_override='class:deep', provider_override='auto' WHERE id=?", (t.id,))
            conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=?", (
                json.dumps({"review_model": "class:deep"}), t.id))
            conn.commit()
        dispatch.dispatch_once(conn, dry_run=False, spawn_fn=lambda *args: seen.append(args))
        stored = kb.get_task(conn, t.id)
        assert seen == []
        assert stored.status == ("triage" if failure == "no_route" else "review")
        assert stored.consecutive_failures == (1 if failure == "no_route" else 0)
        assert stored.claim_lock is None
        event_kind = "gave_up" if failure == "no_route" else "spawn_failed"
        events = [e.payload for e in kb.list_events(conn, t.id) if e.kind == event_kind]
        assert events[-1]["infrastructure"] is True


@pytest.mark.parametrize("receipt", [None, "{", "[]", '{"model": ""}', "wrong_run"])
def test_owner_rejects_missing_or_invalid_current_review_receipt(board, receipt):
    from hermes_cli.kanban_review_model import ReviewModelUnavailable
    config(board)
    with kbc.connect() as conn:
        t = task(conn)
        kb.claim_task(conn, t.id)
        kb.request_review(conn, t.id)
        dispatch.dispatch_once(conn, dry_run=False, spawn_fn=lambda *args: None)
        t = kb.get_task(conn, t.id)
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='model_tier'", (t.id,))
        if receipt is not None:
            kb._append_event(conn, t.id, "model_tier", {"model": OPUS},
                             run_id=t.current_run_id if receipt != "wrong_run" else None)
            if receipt != "wrong_run":
                conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='model_tier'", (receipt, t.id))
            conn.commit()
        with pytest.raises(ReviewModelUnavailable):
            tier.owner_model_pin(conn, t)


@pytest.mark.parametrize("key,value", [
    ("eligible", False), ("funding_verified", False), ("billing", "metered"),
    ("provider", "openai-codex"), ("tools", False), ("streaming", False), ("context_tokens", 100),
])
def test_deep_route_uses_worker_eligibility(board, monkeypatch, key, value):
    from hermes_cli import kanban_model_assignment as assignment
    # No concrete strong model: the registry's eligibility filter alone decides, as the worker applies it.
    cfg = config(board, strong="", routes=["claude-ineligible", FABLE])
    data = json.loads(board.joinpath("config.yaml").read_text(encoding="utf-8"))
    bad = data["model_routing"]["routes"]["deep"][0]
    data["model"] = {"context_length": 10000}
    data["providers"] = {"openai-codex": {"enabled": False}}
    for route in data["model_routing"]["routes"]["deep"]:
        route["capabilities"]["context_tokens"] = 20000
    target = bad["capabilities"] if key in ("tools", "streaming", "context_tokens") else bad
    target[key] = value
    seen = []
    with kbc.connect() as conn:
        t = task(conn, OPUS)
        monkeypatch.setattr(assignment, "_assignee_config", lambda assignee: seen.append(assignee) or data)
        assert tier.resolve_model_tier(conn, t, lane="review", cfg=cfg) == (
            FABLE, "anthropic", "review_unpinned")
    assert seen == ["default"]


@pytest.mark.parametrize("cfg", [{}, {"default_model_profiles": ["other"]}])
def test_pinned_review_not_gated_by_default_tier(board, cfg):
    config(board)
    with kbc.connect() as conn:
        assert tier.resolve_model_tier(conn, task(conn), lane="review", cfg=cfg) == (
            OPUS, "anthropic", "review_unpinned")


@pytest.mark.parametrize("metadata,expected", [
    ({"review_model": SONNET}, (SONNET, "anthropic", "review_override")),
    ({"review_model": "custom", "review_provider": "custom-provider"},
     ("custom", "custom-provider", "review_override")),
    ({"review_model": "class:deep"}, (OPUS, "anthropic", "review_override")),
])
def test_explicit_review_override_is_honored_even_when_it_matches_the_author(board, metadata, expected):
    cfg = config(board)
    with kbc.connect() as conn:
        t = task(conn)
        kb.claim_task(conn, t.id)
        kb.request_review(conn, t.id, metadata=metadata)
        assert tier.resolve_model_tier(conn, t, lane="review", cfg=cfg) == expected
        assert tier.resolve_model_tier(conn, t, lane="ready", cfg=cfg) == (None, None, "pinned")


@pytest.mark.parametrize("metadata", [
    {"review_model": ""}, {"review_model": 7}, {"review_model": OPUS, "review_provider": []},
    {"review_model": "class:deep", "review_provider": "anthropic"},
])
def test_bad_override_fails_closed(board, metadata):
    from hermes_cli.kanban_review_model import ReviewModelUnavailable
    cfg = config(board)
    with kbc.connect() as conn:
        t = task(conn)
        kb.claim_task(conn, t.id)
        kb.request_review(conn, t.id)
        conn.execute("UPDATE task_runs SET metadata=? WHERE task_id=?", (json.dumps(metadata), t.id))
        with pytest.raises(ReviewModelUnavailable):
            tier.apply_model_tier(conn, t, lane="review", cfg=cfg)


def test_disabled_routing_keeps_legacy_pin_but_refuses_explicit_class_override(board):
    from hermes_cli.kanban_review_model import ReviewModelUnavailable
    cfg = config(board, strong="")
    data = json.loads(board.joinpath("config.yaml").read_text(encoding="utf-8"))
    data["model_routing"]["enabled"] = False
    board.joinpath("config.yaml").write_text(json.dumps(data), encoding="utf-8")
    with kbc.connect() as conn:
        t = task(conn, OPUS)
        assert t is not None
        assert tier.resolve_model_tier(conn, t, lane="review", cfg=cfg) == (None, None, "pinned")
        kb.claim_task(conn, t.id)
        assert kb.request_review(conn, t.id, metadata={"review_model": "class:deep"})
        with pytest.raises(ReviewModelUnavailable):
            tier.resolve_model_tier(conn, kb.get_task(conn, t.id), lane="review", cfg=cfg)


def test_unroutable_override_does_not_restore_pin(board, monkeypatch):
    from hermes_cli.kanban_review_model import ReviewModelUnavailable
    cfg = config(board)
    monkeypatch.setattr(tier, "class_unroutable_reason", lambda model, config=None: "routing_disabled")
    with kbc.connect() as conn:
        t = task(conn)
        kb.claim_task(conn, t.id)
        kb.request_review(conn, t.id, metadata={"review_model": "class:deep"})
        with pytest.raises(ReviewModelUnavailable):
            tier.apply_model_tier(conn, t, lane="review", cfg=cfg)


def test_latest_handoff_can_remove_old_override(board):
    cfg = config(board)
    with kbc.connect() as conn:
        t = task(conn)
        kb.claim_task(conn, t.id)
        kb.request_review(conn, t.id, metadata={"review_model": FABLE})
        dispatch.dispatch_once(conn, dry_run=False, spawn_fn=lambda *args: None)
        assert kb.request_changes(conn, t.id, reason="fixture rework")
        kb.claim_task(conn, t.id)
        assert kb.request_review(conn, t.id, metadata={})
        assert tier.resolve_model_tier(conn, kb.get_task(conn, t.id), lane="review", cfg=cfg) == (
            OPUS, "anthropic", "review_unpinned")


@pytest.mark.parametrize("override", [False, True])
def test_skill_review_quota_hold_never_reroutes_back_to_author(board, monkeypatch, override):
    from hermes_cli import kanban_quota_episode as quota
    cfg = config(board)
    wall = {"until": 200, "hinted": True}
    monkeypatch.setattr(quota, "episode", lambda *a: wall)
    monkeypatch.setattr(quota, "_outstanding", lambda *a: [wall])
    monkeypatch.setattr(quota, "_providers", lambda *a: ["anthropic"])
    monkeypatch.setattr(quota, "alternate_route", lambda *a: (SONNET, "anthropic"))
    with kbc.connect() as conn:
        t = task(conn)
        if override:
            kb.claim_task(conn, t.id)
            kb.request_review(conn, t.id, metadata={"review_model": FABLE})
        t.skills = ["sdlc-review"]
        base = tier.resolve_model_tier(conn, t, lane="ready", cfg=cfg)
        assert quota.decision(conn, t, lane="ready", cfg=cfg, base=base, now=100) == (
            "subscription_quota_reset", None)
