"""Kanban default model tier (kanban.default_model) [daveey].

Entry paths covered: dispatcher default for a card with no model (kb.create_task), the ``kanban_create`` tool
with and without ``model``, the review lane, a retry after a failed run, keep patterns, profile scoping, and
the worker argv the pin turns into.
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
    _write_cfg(home)
    return home


def _write_cfg(home: Path, **kanban) -> None:
    route = {"model": OPUS, "provider": "anthropic", "weight": 1,
             "billing": "subscription", "eligible": True, "funding_verified": True,
             "capabilities": {"tools": True, "streaming": True}}
    cfg = {"kanban": kanban, "model_routing": {"enabled": True,
           "routes": {level: [route] for level in ("fast", "standard", "deep")}}}
    home.joinpath("config.yaml").write_text(json.dumps(cfg), encoding="utf-8")


def _review_source(conn, tid):
    """A real dispatched source run and its worker result, before independent handoff.

    No author metadata is asserted by the handoff: custody comes from the source run.
    The spawn seam stands in for an implementation worker completing on Sonnet.
    """
    from hermes_cli.kanban_review_model import implementer_models
    seen = []
    result = kbd.dispatch_once(conn, max_spawn=1, spawn_fn=lambda task, ws: seen.append(task.id))
    assert seen == [tid], result
    task = kb.get_task(conn, tid)
    kb._append_event(conn, tid, "worker_result", {"route_model": SONNET, "route_provider": "anthropic"},
                     run_id=task.current_run_id)
    assert kb.request_review(conn, tid, summary="Implementation ready for independent review")
    task = kb.get_task(conn, tid)
    assert implementer_models(conn, task) == {SONNET}
    assert (task.model_override, task.provider_override) == (None, None)
    return task


def _dispatch(conn) -> dict[str, tuple]:
    """One tick; returns {task_id: (model_override, provider_override, skills)} as the spawn fn saw them."""
    seen: dict[str, tuple] = {}

    def spawn(task, workspace):
        seen[task.id] = (task.model_override, task.provider_override, list(task.skills or []))

    kbd.dispatch_once(conn, dry_run=False, spawn_fn=spawn)
    return seen


def _tier_events(conn, tid):
    return [e.payload for e in kb.list_events(conn, tid) if e.kind == "model_tier"]


def test_dispatcher_default_applies_to_unpinned_card_and_records_event(board):
    _write_cfg(board, default_model=SONNET, default_provider="anthropic")
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine fix", assignee="default")
        seen = _dispatch(conn)
        events = _tier_events(conn, tid)
        row = kb.get_task(conn, tid)
    assert seen[tid][:2] == ("class:standard", "auto")
    assert events and events[0]["reason"] == "default" and events[0]["model"] == "class:standard"
    # The pin is per run: the card row itself stays unpinned.
    assert row.model_override is None


def test_missing_default_uses_standard_class(board):
    _write_cfg(board)  # no default_model
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine fix", assignee="default")
        seen = _dispatch(conn)
        events = _tier_events(conn, tid)
    assert seen[tid][:2] == ("class:standard", "auto")
    assert events[0]["reason"] == "default"


def test_kanban_create_tool_pin_survives_default_and_unpinned_gets_default(board, monkeypatch):
    _write_cfg(board, default_model=SONNET)
    from hermes_cli import models_detect
    monkeypatch.setattr(models_detect, "provider_has_credentials", lambda pid: pid == "anthropic")
    from tools import kanban_tools as kt
    pinned = json.loads(kt._handle_create({"title": "adversarial review", "assignee": "default", "model": "class:deep"}))
    plain = json.loads(kt._handle_create({"title": "routine docs", "assignee": "default"}))
    assert pinned["ok"] and plain["ok"], (pinned, plain)
    with kbc.connect() as conn:
        seen = _dispatch(conn)
        pinned_events = _tier_events(conn, pinned["task_id"])
    assert seen[pinned["task_id"]][0] == "class:deep"
    assert seen[plain["task_id"]][0] == "class:standard"
    assert pinned_events[0]["reason"] == "class" and pinned_events[0]["model"] == "class:deep"
    assert pinned_events[0]["provider"] == "auto"  # class selection remains provider-neutral


def test_review_lane_uses_deep_class(board):
    _write_cfg(board, default_model=SONNET)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="default")
        _review_source(conn, tid)
        seen = _dispatch(conn)
        events = _tier_events(conn, tid)
        assert events[-1]['review_independence'] == 'separate_execution'
        assert 'implementer_models' not in events[-1] and 'implementer_model' not in events[-1]
        from hermes_cli.kanban_review_model import implementer_models
        assert implementer_models(conn, kb.get_task(conn, tid)) == {SONNET}
    assert "sdlc-review" in seen[tid][2]
    assert seen[tid][:2] == (OPUS, "anthropic")
    assert events[-1]["reason"] == "review_unpinned"


def test_retry_after_failed_run_uses_deep_class(board):
    _write_cfg(board, default_model=SONNET)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="flaky fix", assignee="default")
        kb.claim_task(conn, tid)
        # A crashed attempt: the run ends with outcome 'crashed' and the card goes back to ready.
        conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=1 WHERE task_id=?", (tid,))
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, current_run_id=NULL "
                     "WHERE id=?", (tid,))
        conn.commit()
        seen = _dispatch(conn)
        events = _tier_events(conn, tid)
    assert seen[tid][0] == "class:deep"
    assert events[-1]["reason"] == "retry_after_run_crashed"


def test_continued_run_is_not_a_failure(board):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="long job", assignee="default")
        kb.claim_task(conn, tid)
        conn.execute("UPDATE task_runs SET status='released', outcome='continued', ended_at=1 WHERE task_id=?", (tid,))
        conn.commit()
        task = kb.get_task(conn, tid)
        task.current_run_id = None
        model, _prov, reason = tier.resolve_model_tier(conn, task, lane="ready", cfg={"default_model": SONNET})
    assert (model, reason) == ("class:standard", "default")


def test_keep_patterns_and_profile_scope(board):
    cfg = {"default_model": SONNET, "default_model_keep_patterns": [r"\bsecurity\b", "hermes-core-wt"],
           "default_model_profiles": ["default"]}
    with kbc.connect() as conn:
        sec = kb.get_task(conn, kb.create_task(conn, title="Self-fix SECURITY tiers", assignee="default"))
        core = kb.get_task(conn, kb.create_task(conn, title="x", body="build in hermes-core-wt", assignee="default"))
        other = kb.get_task(conn, kb.create_task(conn, title="routine", assignee="bb-dev"))
        plain = kb.get_task(conn, kb.create_task(conn, title="routine", assignee="default"))
        got = {name: tier.resolve_model_tier(conn, t, lane="ready", cfg=cfg)
               for name, t in (("sec", sec), ("core", core), ("other", other), ("plain", plain))}
    assert got["sec"][0] == "class:deep" and got["sec"][2].startswith("pattern:")
    assert got["core"][0] == "class:deep" and got["core"][2] == "pattern:hermes-core-wt"
    assert got["other"] == ("class:standard", "auto", "profile_out_of_scope")
    assert got["plain"] == ("class:standard", "auto", "default")


def test_bad_pattern_is_ignored_not_fatal(board):
    with kbc.connect() as conn:
        t = kb.get_task(conn, kb.create_task(conn, title="routine", assignee="default"))
        got = tier.resolve_model_tier(conn, t, lane="ready",
                                      cfg={"default_model": SONNET, "default_model_keep_patterns": ["("]})
    assert got == ("class:standard", "auto", "default")


def test_tier_pin_reaches_owner(board):
    """The tier pin lives on the in-memory claimed task; the profile owner (workers are
    ``-m hermes_cli.kanban_worker_client``) re-reads the card and takes it from the run's model_tier event."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine", assignee="default")
        t = kb.claim_task(conn, tid)
        tier.apply_model_tier(conn, t, lane="ready", cfg={"default_model": SONNET, "default_provider": "anthropic"})
        fresh = kb.get_task(conn, tid)
        assert (fresh.model_override, fresh.provider_override) == (None, None)  # never the DB row
        assert tier.owner_model_pin(conn, fresh) == ("class:standard", "auto")
        # Another run of the same card does not inherit this run's tier decision.
        fresh.current_run_id = (fresh.current_run_id or 0) + 1000
        assert tier.owner_model_pin(conn, fresh) == ("class:standard", "auto")


def test_owner_runs_class_selector_with_provider_auto(board):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="deep", assignee="default", model_override="class:deep")
        assert tier.owner_model_pin(conn, kb.get_task(conn, tid)) == ("class:deep", "auto")


def test_legacy_default_model_and_provider_cannot_pin_dispatch(board):
    """default_model claude-* with no default_provider ran on the profile's provider (openai-codex after the
    profile switched to a GPT): HTTP 400 on every card. The tier now names the model's own provider."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="plain job", assignee="default")
        task = kb.get_task(conn, tid)
        model, prov, reason = tier.resolve_model_tier(conn, task, lane="ready", cfg={"default_model": SONNET})
        assert (model, prov, reason) == ("class:standard", "auto", "default")
        # A concrete default_provider cannot constrain the neutral default.
        _m, prov, _r = tier.resolve_model_tier(conn, task, lane="ready",
                                               cfg={"default_model": SONNET, "default_provider": "openrouter"})
        assert prov == "auto"
    assert tier.infer_provider("some-unknown-model") is None
