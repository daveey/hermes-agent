"""Strong classes remain neutral; reviews allocate independent concrete run receipts. [daveey]"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_model_tier as tier
from tests.hermes_cli.test_kanban_model_tier import _review_source, _write_cfg

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


def _cfg(**extra):
    return {"default_model": "class:standard", "default_provider": "auto", **extra}


def _legacy_pin(conn, tid, model, provider):
    """Historical row seeded raw in this isolated test DB: ingress refuses concrete pins."""
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
        review = _review_source(conn, kb.create_task(conn, title="impl", assignee="default"))
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
    """Retired config cannot override the independently allocated reviewer."""
    with kbc.connect() as conn:
        t = _review_source(conn, kb.create_task(conn, title="impl", assignee="default"))
        assert tier.resolve_model_tier(conn, t, lane="review", cfg=_cfg(strong_model=SONNET)) == (
            OPUS, "anthropic", "review_unpinned")
        assert tier.resolve_model_tier(conn, t, lane="review",
                                       cfg=_cfg(strong_model=SONNET, strong_provider="openai-codex")) == (
            OPUS, "anthropic", "review_unpinned")


def test_strong_model_unset_keeps_class_deep(board):
    with kbc.connect() as conn:
        t = _review_source(conn, kb.create_task(conn, title="impl", assignee="default"))
        assert tier.resolve_model_tier(conn, t, lane="review", cfg=_cfg(strong_model="")) == (
            OPUS, "anthropic", "review_unpinned")
        assert tier.resolve_model_tier(conn, t, lane="review", cfg={"default_model": "class:standard"})[:2] == (
            OPUS, "anthropic")


def test_class_pins_legacy_pins_and_out_of_scope_profiles_use_class_routes(board):
    cfg = _cfg(strong_model="class:deep", default_model_profiles=["default"])
    # Bind the assignee's own registry rather than bypassing profile eligibility.
    profile = board / "profiles" / "bb-dev"
    profile.mkdir(parents=True)
    profile.joinpath("config.yaml").write_text(board.joinpath("config.yaml").read_text())
    with kbc.connect() as conn:
        other = _review_source(conn, kb.create_task(conn, title="impl", assignee="bb-dev"))
        classed = kb.get_task(conn, kb.create_task(conn, title="impl", assignee="default", model_override="class:fast",
                                                   provider_override="auto"))
        legacy = _legacy_pin(conn, kb.create_task(conn, title="old", assignee="default"), SONNET, "anthropic")
        with pytest.raises(ValueError):
            kb.create_task(conn, title="no", assignee="default", model_override=SONNET, provider_override="anthropic")
        assert tier.resolve_model_tier(conn, classed, lane="ready", cfg=cfg) == ("class:fast", "auto", "class")
        assert tier.resolve_model_tier(conn, legacy, lane="ready", cfg=cfg) == ("class:standard", "auto", "default")
        assert (legacy.model_override, legacy.provider_override) == (SONNET, "anthropic")
        assert tier.resolve_model_tier(conn, other, lane="review", cfg=cfg) == (
            OPUS, "anthropic", "review_unpinned")


def test_dispatcher_review_lane_runs_on_strong_class_and_records_it(board):
    _write_cfg(board, **_cfg(strong_model="class:deep"))
    seen = {}
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="default")
        _review_source(conn, tid)
        kbd.dispatch_once(conn, dry_run=False,
                          spawn_fn=lambda task, ws: seen.__setitem__(task.id, (task.model_override,
                                                                               task.provider_override)))
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == "model_tier"]
        row = kb.get_task(conn, tid)
        assert (row.model_override, row.provider_override) == (None, None)
    assert seen[tid] == (OPUS, "anthropic")
    assert events[-1]["reason"] == "review_unpinned" and events[-1]["model"] == OPUS
    assert events[-1]["review_independence"] == "separate_execution"
