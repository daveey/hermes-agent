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
    return home


def _write_cfg(home: Path, **kanban) -> None:
    lines = ["kanban:"]
    for k, v in kanban.items():
        lines.append(f"  {k}: {json.dumps(v)}")
    home.joinpath("config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    assert seen[tid][:2] == (SONNET, "anthropic")
    assert events and events[0]["reason"] == "default" and events[0]["model"] == SONNET
    # The pin is per run: the card row itself stays unpinned.
    assert row.model_override is None


def test_dispatcher_default_off_is_upstream_behaviour(board):
    _write_cfg(board)  # no default_model
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine fix", assignee="default")
        seen = _dispatch(conn)
        events = _tier_events(conn, tid)
    assert seen[tid][:2] == (None, None)
    assert events == []


def test_kanban_create_tool_pin_survives_default_and_unpinned_gets_default(board, monkeypatch):
    _write_cfg(board, default_model=SONNET)
    from tools import kanban_tools as kt
    pinned = json.loads(kt._handle_create({"title": "adversarial review", "assignee": "default", "model": OPUS}))
    plain = json.loads(kt._handle_create({"title": "routine docs", "assignee": "default"}))
    assert pinned["ok"] and plain["ok"], (pinned, plain)
    with kbc.connect() as conn:
        seen = _dispatch(conn)
        pinned_events = _tier_events(conn, pinned["task_id"])
    assert seen[pinned["task_id"]][0] == OPUS
    assert seen[plain["task_id"]][0] == SONNET
    assert pinned_events[0]["reason"] == "pinned" and pinned_events[0]["model"] == OPUS


def test_review_lane_keeps_profile_model(board):
    _write_cfg(board, default_model=SONNET)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="default")
        kb.claim_task(conn, tid)
        kb.request_review(conn, tid, summary="done, please review")
        seen = _dispatch(conn)
        events = _tier_events(conn, tid)
    assert "sdlc-review" in seen[tid][2]
    assert seen[tid][0] is None
    assert events[-1]["reason"] == "review"


def test_retry_after_failed_run_keeps_profile_model(board):
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
    assert seen[tid][0] is None
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
    assert (model, reason) == (SONNET, "default")


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
    assert got["sec"][0] is None and got["sec"][2].startswith("pattern:")
    assert got["core"][0] is None and got["core"][2] == "pattern:hermes-core-wt"
    assert got["other"] == (None, None, "profile_out_of_scope")
    assert got["plain"] == (SONNET, "anthropic", "default")


def test_bad_pattern_is_ignored_not_fatal(board):
    with kbc.connect() as conn:
        t = kb.get_task(conn, kb.create_task(conn, title="routine", assignee="default"))
        got = tier.resolve_model_tier(conn, t, lane="ready",
                                      cfg={"default_model": SONNET, "default_model_keep_patterns": ["("]})
    assert got == (SONNET, "anthropic", "default")


def test_tier_pin_reaches_worker_argv(board, monkeypatch):
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda home: None)
    with kbc.connect() as conn:
        t = kb.get_task(conn, kb.create_task(conn, title="routine", assignee="default"))
        tier.apply_model_tier(conn, t, lane="ready", cfg={"default_model": SONNET, "default_provider": "anthropic"})
    argv = kbd._worker_argv(t, "default", None)
    assert argv[argv.index("-m") + 1] == SONNET
    assert argv[argv.index("--provider") + 1] == "anthropic"


def test_unset_default_provider_is_inferred_not_the_profiles(board):
    """default_model claude-* with no default_provider ran on the profile's provider (openai-codex after the
    profile switched to a GPT): HTTP 400 on every card. The tier now names the model's own provider."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="plain job", assignee="default")
        task = kb.get_task(conn, tid)
        model, prov, reason = tier.resolve_model_tier(conn, task, lane="ready", cfg={"default_model": SONNET})
        assert (model, prov, reason) == (SONNET, "anthropic", "default")
        # An explicit default_provider still wins.
        _m, prov, _r = tier.resolve_model_tier(conn, task, lane="ready",
                                               cfg={"default_model": SONNET, "default_provider": "openrouter"})
        assert prov == "openrouter"
    assert tier.infer_provider("some-unknown-model") is None
