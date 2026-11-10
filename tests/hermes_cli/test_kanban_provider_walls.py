"""Provider quota walls: record on rate-limited exit, re-tier at claim, storm alert [daveey]."""
from __future__ import annotations

import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_provider_walls as walls
from hermes_cli.kanban_model_tier import apply_model_tier

from tests.hermes_cli.test_kanban_model_tier import (
    OPUS, SONNET, _dispatch, _tier_events, _write_cfg, board as _board_fixture,
)

board = _board_fixture

CODEX = "openai-codex"


def _wall(board, provider=CODEX, seconds=524389):
    walls.record_wall(provider, seconds, path=board / "kanban" / "provider_walls.json")


def test_parse_retry_after_takes_largest():
    assert walls.parse_retry_after("quota exhausted (429); retry after 524389s") == 524389
    assert walls.parse_retry_after("nothing here") is None


def test_short_wall_not_tracked(board):
    assert walls.record_wall(CODEX, 60, path=board / "w.json") is None
    assert walls.active_walls(path=board / "w.json") == {}


def test_expired_wall_ignored(board):
    p = board / "w.json"
    walls.record_wall(CODEX, 3600, now=time.time() - 7200, path=p)
    assert walls.active_walls(path=p) == {}


def test_keep_pattern_card_preserves_strong_route_under_wall(board, monkeypatch):
    _write_cfg(board, default_model=SONNET, default_provider="anthropic", default_model_keep_patterns=["security"])
    monkeypatch.setattr(walls, "profile_provider", lambda: CODEX)
    monkeypatch.setattr(walls, "walls_path", lambda: board / "w.json")
    walls.record_wall(CODEX, 524389, path=board / "w.json")
    conn = kbc.connect()
    tid = kb.create_task(conn, title="security audit", assignee="default")
    seen = _dispatch(conn)
    assert seen[tid][:2] == (None, None)
    assert _tier_events(conn, tid)[-1]['reason'] == 'pattern:security'


def test_pinned_card_on_walled_provider_keeps_exact_pin(board, monkeypatch):
    _write_cfg(board, default_model=SONNET, default_provider="anthropic")
    monkeypatch.setattr(walls, "walls_path", lambda: board / "w.json")
    walls.record_wall(CODEX, 524389, path=board / "w.json")
    conn = kbc.connect()
    a = kb.create_task(conn, title="a", assignee="default", model_override="gpt-6-astra", provider_override=CODEX)
    b = kb.create_task(conn, title="b require_model", assignee="default", model_override="gpt-6-astra",
                       provider_override=CODEX)
    seen = _dispatch(conn)
    assert seen[a][:2] == ("gpt-6-astra", CODEX)
    assert seen[b][:2] == ("gpt-6-astra", CODEX)


def test_no_wall_no_change(board, monkeypatch):
    _write_cfg(board, default_model=SONNET, default_provider="anthropic", default_model_keep_patterns=["security"])
    monkeypatch.setattr(walls, "walls_path", lambda: board / "w.json")
    monkeypatch.setattr(walls, "profile_provider", lambda: CODEX)
    conn = kbc.connect()
    tid = kb.create_task(conn, title="security audit", assignee="default")
    assert _dispatch(conn)[tid][0] is None


def test_fallback_also_walled_leaves_task(board, monkeypatch):
    _write_cfg(board, default_model=SONNET, default_provider="anthropic")
    monkeypatch.setattr(walls, "walls_path", lambda: board / "w.json")
    _wall(board); walls.record_wall("anthropic", 7200, path=board / "w.json")
    class T: model_override, provider_override, title, body = "x", CODEX, "t", ""
    assert walls.retier_for_walls(T(), {"default_model": SONNET, "default_provider": "anthropic"},
                                  path=board / "w.json") is None


def test_record_wall_for_run_uses_tier_event_and_log(board, monkeypatch):
    p = board / "w.json"
    monkeypatch.setattr(walls, "walls_path", lambda: p)
    conn = kbc.connect()
    tid = kb.create_task(conn, title="x", assignee="default")
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "model_tier", {"provider": CODEX, "reason": "default"}, run_id=None)
    log = board / "t.log"
    log.write_text("Codex provider quota exhausted (429); retry after 524389s")
    assert walls.record_wall_for_run(conn, tid, None, log)
    assert CODEX in walls.active_walls(path=p)


def test_storm_alert_opens_one_local_card_with_worker_pool(board, monkeypatch):
    from hermes_cli import kanban_placement as placement
    monkeypatch.setattr(placement, "config", lambda: {"worker_pools": {"hub": ["worker-1"]}})
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    conn = kbc.connect()
    now = int(time.time())
    for i in range(12):
        conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
                     "VALUES ('t_x', 'default', 'rate_limited', 'rate_limited', ?, ?)", (now - 60, now - 30))
    conn.commit()
    first = walls.alert_on_storm(conn)
    assert first and walls.alert_on_storm(conn) == first
    assert kb.get_task(conn, first).placement_policy is None


def test_storm_alert_quiet_below_threshold(board):
    assert walls.alert_on_storm(kbc.connect()) is None


def test_default_tier_on_walled_default_provider_moves_to_strong_tier(board, monkeypatch):
    """2026-11-01: default_model was gpt-6-astra/openai-codex; a Codex wall had only Codex as fallback."""
    _write_cfg(board, default_model="gpt-6-astra", default_provider=CODEX,
               strong_model=OPUS, strong_provider="anthropic")
    monkeypatch.setattr(walls, "walls_path", lambda: board / "w.json")
    walls.record_wall(CODEX, 200714, path=board / "w.json")
    conn = kbc.connect()
    tid = kb.create_task(conn, title="plain card", assignee="default")
    seen = _dispatch(conn)
    assert seen[tid][:2] == (OPUS, "anthropic")
    assert "wall:openai-codex" in _tier_events(conn, tid)[-1]["reason"]


def test_live_pool_exhaustion_counts_as_wall_without_a_crash(board, monkeypatch):
    _write_cfg(board, default_model="gpt-6-astra", default_provider=CODEX,
               strong_model=OPUS, strong_provider="anthropic")
    monkeypatch.setattr(walls, "walls_path", lambda: board / "w.json")
    monkeypatch.setattr(walls, "live_quota_wall",
                        lambda p, m: time.time() + 200714 if p == CODEX else None)
    conn = kbc.connect()
    tid = kb.create_task(conn, title="plain card", assignee="default")
    pinned = kb.create_task(conn, title="pinned", assignee="default", model_override="gpt-6-astra",
                            provider_override=CODEX)
    seen = _dispatch(conn)
    assert seen[tid][:2] == (OPUS, "anthropic")
    assert seen[pinned][:2] == ("gpt-6-astra", CODEX)  # exact pins keep their tuple


def test_every_fallback_exhausted_leaves_task_on_its_route(board, monkeypatch):
    _write_cfg(board, default_model="gpt-6-astra", default_provider=CODEX,
               strong_model=OPUS, strong_provider="anthropic")
    monkeypatch.setattr(walls, "walls_path", lambda: board / "w.json")
    monkeypatch.setattr(walls, "live_quota_wall", lambda p, m: time.time() + 3600)
    conn = kbc.connect()
    tid = kb.create_task(conn, title="plain card", assignee="default")
    assert _dispatch(conn)[tid][:2] == ("gpt-6-astra", CODEX)


def test_live_quota_wall_reads_the_oauth_pool(monkeypatch):
    import agent.credential_pool as cp
    import agent.model_routing_unavailable as mru
    calls = []
    monkeypatch.setattr(cp, "load_pool", lambda provider, allowed_auth_types=None: calls.append(
        (provider, allowed_auth_types)) or object())
    monkeypatch.setattr(mru, "quota_unavailable",
                        lambda pool, model: mru.SubscriptionQuotaUnavailable(CODEX, model, 1793774149.0))
    assert walls.live_quota_wall(CODEX, "gpt-6-astra") == 1793774149.0
    assert calls == [(CODEX, {"oauth"})]
    monkeypatch.setattr(mru, "quota_unavailable", lambda pool, model: None)
    assert walls.live_quota_wall(CODEX, "gpt-6-astra") is None
    assert walls.live_quota_wall("auto", "gpt-6-astra") is None
    assert walls.live_quota_wall(CODEX, "class:deep") is None
