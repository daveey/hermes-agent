"""Kanban strong tier is a class selector (kanban.strong_model) [daveey].

Review-lane, retry-after-failure and keep-pattern cards run on the strong class (``class:deep``) instead of
following the profile's default. Kanban is model-neutral: concrete ``strong_model``/``strong_provider`` names
are retired config and are ignored; only class selectors route. Entry paths: resolve_model_tier for each
branch, and the dispatcher's ``model_tier`` event plus the claimed task's pin for a review-lane card.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_model_tier as tier

SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _cfg(**extra):
    return {"default_model": "class:standard", "default_provider": "auto", **extra}


def _legacy_pin(conn, tid, model, provider):
    """Historical row seeded raw in this isolated test DB: ingress now refuses concrete pins."""
    conn.execute("UPDATE tasks SET model_override=?, provider_override=? WHERE id=?", (model, provider, tid))
    conn.commit()
    return kb.get_task(conn, tid)


def _fail_once(conn, tid):
    kb.claim_task(conn, tid)
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=1 WHERE task_id=?", (tid,))
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, current_run_id=NULL "
                 "WHERE id=?", (tid,))
    conn.commit()


def test_strong_class_for_review_retry_and_keep_pattern(board):
    cfg = _cfg(strong_model="class:deep", default_model_keep_patterns=["hermes-core-wt"])
    with kbc.connect() as conn:
        review = kb.get_task(conn, kb.create_task(conn, title="impl", assignee="default"))
        retry_id = kb.create_task(conn, title="flaky fix", assignee="default")
        _fail_once(conn, retry_id)
        retry = kb.get_task(conn, retry_id)
        keep = kb.get_task(conn, kb.create_task(conn, title="x", body="build in hermes-core-wt", assignee="default"))
        plain = kb.get_task(conn, kb.create_task(conn, title="routine", assignee="default"))
        got_review = tier.resolve_model_tier(conn, review, lane="review", cfg=cfg)
        got_retry = tier.resolve_model_tier(conn, retry, lane="ready", cfg=cfg)
        got_keep = tier.resolve_model_tier(conn, keep, lane="ready", cfg=cfg)
        got_plain = tier.resolve_model_tier(conn, plain, lane="ready", cfg=cfg)
    assert got_review == (OPUS, "anthropic", "review_unpinned")
    assert got_retry == ("class:deep", "auto", "retry_after_run_crashed")
    assert got_keep == ("class:deep", "auto", "pattern:hermes-core-wt")
    assert got_plain == ("class:standard", "auto", "default")


def test_concrete_strong_model_and_provider_config_are_ignored(board):
    """Retired ``strong_model``/``strong_provider`` names never route: the class tier decides."""
    with kbc.connect() as conn:
        t = kb.get_task(conn, kb.create_task(conn, title="impl", assignee="default"))
        assert tier.resolve_model_tier(conn, t, lane="review", cfg=_cfg(strong_model=OPUS)) == (
            "class:deep", "auto", "review")
        assert tier.resolve_model_tier(conn, t, lane="review",
                                       cfg=_cfg(strong_model=OPUS, strong_provider="anthropic")) == (
            "class:deep", "auto", "review")


def test_strong_model_unset_keeps_class_deep(board):
    with kbc.connect() as conn:
        t = kb.get_task(conn, kb.create_task(conn, title="impl", assignee="default"))
        assert tier.resolve_model_tier(conn, t, lane="review", cfg=_cfg(strong_model="")) == (
            "class:deep", "auto", "review")
        assert tier.resolve_model_tier(conn, t, lane="review", cfg={"default_model": "class:standard"})[:2] == (
            "class:deep", "auto")


def test_class_pins_legacy_pins_and_out_of_scope_profiles_use_class_routes(board):
    cfg = _cfg(strong_model="class:deep", default_model_profiles=["default"])
    with kbc.connect() as conn:
        classed = kb.get_task(conn, kb.create_task(conn, title="impl", assignee="default", model_override="class:fast",
                                                   provider_override="auto"))
        legacy = _legacy_pin(conn, kb.create_task(conn, title="old", assignee="default"), SONNET, "anthropic")
        other = kb.get_task(conn, kb.create_task(conn, title="impl", assignee="bb-dev"))
        with pytest.raises(ValueError):
            kb.create_task(conn, title="no", assignee="default", model_override=SONNET, provider_override="anthropic")
        assert tier.resolve_model_tier(conn, classed, lane="ready", cfg=cfg) == ("class:fast", "auto", "class")
        # The retired concrete pin is ignored (routes as an unpinned default); the author row keeps it.
        assert tier.resolve_model_tier(conn, legacy, lane="ready", cfg=cfg) == ("class:standard", "auto", "default")
        assert (legacy.model_override, legacy.provider_override) == (SONNET, "anthropic")
        assert tier.resolve_model_tier(conn, other, lane="review", cfg=cfg) == (
            "class:standard", "auto", "profile_out_of_scope")


def test_dispatcher_review_lane_runs_on_strong_class_and_records_it(board):
    route = {"model": OPUS, "provider": "anthropic", "weight": 1, "billing": "subscription", "eligible": True,
             "funding_verified": True, "capabilities": {"tools": True, "streaming": True}}
    board.joinpath("config.yaml").write_text(json.dumps({
        "kanban": _cfg(strong_model="class:deep"),
        "model_routing": {"enabled": True, "routes": {lvl: [route] for lvl in ("fast", "standard", "deep")}},
    }), encoding="utf-8")
    seen = {}
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="default")
        kb.claim_task(conn, tid)
        kb.request_review(conn, tid, summary="done, please review")
        kbd.dispatch_once(conn, dry_run=False,
                          spawn_fn=lambda task, ws: seen.__setitem__(task.id, (task.model_override,
                                                                               task.provider_override)))
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == "model_tier"]
    assert seen[tid] == (OPUS, "anthropic")
    assert events[-1]["reason"] == "review_unpinned" and events[-1]["model"] == OPUS
