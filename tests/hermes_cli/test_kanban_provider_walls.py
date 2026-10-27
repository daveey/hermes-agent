"""Provider quota walls: record on rate-limited exit, re-tier at claim, storm alert [daveey]."""
from __future__ import annotations

import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_provider_walls as walls
from hermes_cli.kanban_model_tier import apply_model_tier

from tests.hermes_cli.test_kanban_model_tier import (
    OPUS, SONNET, _dispatch, _tier_events, _write_cfg, board,
)

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


def test_keep_pattern_card_retiered_off_walled_profile_provider(board, monkeypatch):
    _write_cfg(board, default_model=SONNET, default_provider="anthropic", default_model_keep_patterns=["security"])
    monkeypatch.setattr(walls, "profile_provider", lambda: CODEX)
    monkeypatch.setattr(walls, "walls_path", lambda: board / "w.json")
    walls.record_wall(CODEX, 524389, path=board / "w.json")
    conn = kbc.connect()
    tid = kb.create_task(conn, title="security audit", assignee="default")
    seen = _dispatch(conn)
    assert seen[tid][:2] == (SONNET, "anthropic")
    assert "wall:openai-codex" in _tier_events(conn, tid)[-1]["reason"]


def test_pinned_card_on_walled_provider_moves_unless_require_model(board, monkeypatch):
    _write_cfg(board, default_model=SONNET, default_provider="anthropic")
    monkeypatch.setattr(walls, "walls_path", lambda: board / "w.json")
    walls.record_wall(CODEX, 524389, path=board / "w.json")
    conn = kbc.connect()
    a = kb.create_task(conn, title="a", assignee="default", model_override="gpt-6-astra", provider_override=CODEX)
    b = kb.create_task(conn, title="b require_model", assignee="default", model_override="gpt-6-astra",
                       provider_override=CODEX)
    seen = _dispatch(conn)
    assert seen[a][:2] == (SONNET, "anthropic")
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


def test_storm_alert_opens_one_card(board):
    conn = kbc.connect()
    now = int(time.time())
    for i in range(12):
        conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
                     "VALUES ('t_x', 'default', 'rate_limited', 'rate_limited', ?, ?)", (now - 60, now - 30))
    conn.commit()
    first = walls.alert_on_storm(conn)
    assert first and walls.alert_on_storm(conn) == first


def test_storm_alert_quiet_below_threshold(board):
    assert walls.alert_on_storm(kbc.connect()) is None
