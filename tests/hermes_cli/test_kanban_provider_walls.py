"""Provider quota walls: record on rate-limited exit, re-tier at claim, storm alert [daveey]."""
from __future__ import annotations

import json
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
    b = kb.create_task(conn, title="b", body="must stay on astra\nrequire_model: true", assignee="default",
                       model_override="gpt-6-astra", provider_override=CODEX)
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
    _rate_limited_runs(conn, 12, claimer=kb._claimer_id(), now=now)
    first = walls.alert_on_storm(conn)
    assert first and walls.alert_on_storm(conn) == first
    assert kb.get_task(conn, first).placement_policy is None


def test_storm_alert_quiet_below_threshold(board):
    assert walls.alert_on_storm(kbc.connect()) is None


def _rate_limited_runs(conn, n, *, claimer, now, in_lock=True):
    for _ in range(n):
        meta = json.dumps({"claimer": claimer, "exit_code": 75})
        conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, claim_lock, "
                     "metadata) VALUES ('t_x', 'default', 'rate_limited', 'rate_limited', ?, ?, ?, ?)",
                     (now - 60, now - 30, claimer if in_lock else None, meta))
    conn.commit()


def test_storm_counts_only_this_hosts_runs(board):
    """2026-11-12: worker-5 filed '11 … on this host' for runs mostly claimed by worker-6 and worker-2."""
    conn = kbc.connect()
    now = int(time.time())
    mine = kb._host_prefix()
    _rate_limited_runs(conn, 3, claimer=f"{mine}111", now=now)
    _rate_limited_runs(conn, 2, claimer=f"{mine}222", now=now, in_lock=False)  # synced row: claimer in metadata
    _rate_limited_runs(conn, 20, claimer="hermes-other-host:999", now=now, in_lock=False)
    _rate_limited_runs(conn, 4, claimer=f"x{mine}5", now=now)  # a host whose name merely contains ours
    conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, metadata) "
                 "VALUES ('t_x', 'default', 'rate_limited', 'rate_limited', ?, ?, 'not json')", (now - 60, now - 30))
    conn.commit()
    assert walls.rate_limit_storm(conn, now=now) == 5
    assert walls.rate_limit_storm(conn, now=now, prefix="hermes-other-host:") == 20
    assert walls.alert_on_storm(conn, now=now) is None


def test_storm_alert_body_does_not_opt_itself_out_of_wall_reroute(board):
    """2026-11-12: the alert body named `require_model`, so its first run skipped the reroute into the wall."""
    conn = kbc.connect()
    now = int(time.time())
    _rate_limited_runs(conn, 12, claimer=kb._claimer_id(), now=now)
    tid = walls.alert_on_storm(conn, now=now)
    assert tid
    task = kb.get_task(conn, tid)
    assert not walls.requires_model(task)
    from hermes_cli import kanban_pin_reroute
    assert not kanban_pin_reroute.is_mandatory_pin(task)


def test_requires_model_needs_a_deliberate_line():
    class T:
        title = "require_model"
        def __init__(self, body): self.body = body
    assert not walls.requires_model(T("cards pinned to the walled provider (`require_model`)."))
    assert not walls.requires_model(T("require_model: false"))
    assert not walls.requires_model(T(None))
    assert walls.requires_model(T("must stay on astra\nrequire_model: true\n"))
    assert walls.requires_model(T(f"  {walls.REQUIRE_MODEL_MARKER.upper()}  "))


def test_storm_alert_prints_wall_date_in_utc(board, monkeypatch):
    """2026-11-12: 'openai-codex until 05:58' hid that the wall lasted 5.8 more days."""
    import calendar
    import os
    p = board / "w.json"
    monkeypatch.setattr(walls, "walls_path", lambda: p)
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"  # a local-time render would say 2099-11-17 21:59
    time.tzset()
    try:
        now = int(time.time())
        until = calendar.timegm((2099, 11, 18, 5, 59, 0))
        p.write_text(json.dumps({"openai-codex": until}))
        conn = kbc.connect()
        _rate_limited_runs(conn, 12, claimer=kb._claimer_id(), now=now)
        tid = walls.alert_on_storm(conn, now=now)
        assert tid
        body = kb.get_task(conn, tid).body or ""
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    assert "openai-codex until 2099-11-18 05:59Z" in body


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
    # Exact pins keep their tuple: never rewritten. This fresh one has no same-class peer ("default" has no
    # routing policy), so it is held on the live wall (kanban_dispatch_skip); with a free class peer it is
    # rerouted instead (test_kanban_pin_reroute::test_fresh_pin_on_a_far_live_wall_reroutes_on_its_first_run).
    assert pinned not in seen
    assert [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ? AND kind = 'dispatch_skipped'",
                                       (pinned,))] == ["dispatch_skipped"]


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
