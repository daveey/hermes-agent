"""Kanban strong tier names its model (kanban.strong_model / strong_provider) [daveey].

Review-lane, retry-after-failure and keep-pattern cards used to run on "the profile's model", so a change of
the profile default silently moved every strong run with it. With ``strong_model`` set they run on that model;
unset keeps the old behaviour. Entry paths: resolve_model_tier for each branch, and the dispatcher's
``model_tier`` event plus the claimed task's pin for a review-lane card.
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
    return {"default_model": SONNET, "default_provider": "anthropic", **extra}


def _fail_once(conn, tid):
    kb.claim_task(conn, tid)
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=1 WHERE task_id=?", (tid,))
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, claim_expires=NULL, current_run_id=NULL "
                 "WHERE id=?", (tid,))
    conn.commit()


def test_strong_model_for_review_retry_and_keep_pattern(board):
    cfg = _cfg(strong_model=OPUS, strong_provider="anthropic", default_model_keep_patterns=["hermes-core-wt"])
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
    assert got_review == (OPUS, "anthropic", "review")
    assert got_retry == (OPUS, "anthropic", "retry_after_run_crashed")
    assert got_keep == (OPUS, "anthropic", "pattern:hermes-core-wt")
    assert got_plain == (SONNET, "anthropic", "default")


def test_strong_provider_unset_is_inferred_from_strong_model(board):
    with kbc.connect() as conn:
        t = kb.get_task(conn, kb.create_task(conn, title="impl", assignee="default"))
        assert tier.resolve_model_tier(conn, t, lane="review", cfg=_cfg(strong_model=OPUS)) == (
            OPUS, "anthropic", "review")


def test_strong_model_unset_keeps_old_behaviour(board):
    with kbc.connect() as conn:
        t = kb.get_task(conn, kb.create_task(conn, title="impl", assignee="default"))
        assert tier.resolve_model_tier(conn, t, lane="review", cfg=_cfg(strong_model="")) == (None, None, "review")
        assert tier.resolve_model_tier(conn, t, lane="review", cfg={"default_model": "class:standard"})[:2] == (
            "class:deep", "auto")


def test_pins_and_out_of_scope_profiles_ignore_strong_model(board):
    cfg = _cfg(strong_model=OPUS, default_model_profiles=["default"])
    with kbc.connect() as conn:
        pinned = kb.get_task(conn, kb.create_task(conn, title="impl", assignee="default", model_override=SONNET,
                                                      provider_override="anthropic"))
        other = kb.get_task(conn, kb.create_task(conn, title="impl", assignee="bb-dev"))
        assert tier.resolve_model_tier(conn, pinned, lane="ready", cfg=cfg) == (None, None, "pinned")
        assert tier.resolve_model_tier(conn, pinned, lane="review", cfg=cfg) == (OPUS, "anthropic", "review_unpinned")
        assert tier.resolve_model_tier(conn, other, lane="review", cfg=cfg) == (None, None, "profile_out_of_scope")


def test_dispatcher_review_lane_runs_on_strong_model_and_records_it(board):
    board.joinpath("config.yaml").write_text(
        "kanban:\n" + "".join(f"  {k}: {json.dumps(v)}\n" for k, v in _cfg(strong_model=OPUS).items()),
        encoding="utf-8")
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
    assert events[-1]["reason"] == "review" and events[-1]["model"] == OPUS
