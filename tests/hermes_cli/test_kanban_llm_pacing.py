"""LLM quota pacing governor: pool math and Kanban dispatch admission (t_01a1d975f57bb7b8). [daveey]"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd

from tests.hermes_cli.test_kanban_model_tier import _write_cfg, board as _board_fixture

board = _board_fixture

OPUS = "claude-opus-5-5"
HOUR, DAY = 3600.0, 86400.0
CFG = lp.PacingConfig()


def _r(account, kind, used, reset_in, ts):
    return lp.Reading(ts, account, kind, used, ts + reset_in)


# --- pool math ---------------------------------------------------------------------------------------------

def test_pool_ahead_of_pace_is_paced():
    now = 1_800_000_000.0
    # 60% used, 10 h to reset, burning 6%/h over the last hour: 120% projected at reset.
    current = [_r("a", lp.SEVEN_DAY, 60, 10 * HOUR, now)]
    history = [_r("a", lp.SEVEN_DAY, 54, 11 * HOUR, now - HOUR)]
    pace = lp.pool_pace("anthropic", current, history, CFG)
    assert pace.state == lp.PACED
    assert pace.factor == pytest.approx((35 / (10 * HOUR)) / (6 / HOUR), rel=1e-3)


def test_pool_behind_pace_is_open():
    now = 1_800_000_000.0
    current = [_r("a", lp.SEVEN_DAY, 30, 3 * DAY, now), _r("a", lp.FIVE_HOUR, 10, 4 * HOUR, now)]
    history = [_r("a", lp.SEVEN_DAY, 29.5, 3 * DAY + HOUR, now - HOUR), _r("a", lp.FIVE_HOUR, 8, 5 * HOUR, now - HOUR)]
    assert lp.pool_pace("anthropic", current, history, CFG).state == lp.OPEN


def test_behind_elapsed_pace_but_projected_over_is_paced():
    """askevepodcast 11-14: 87% at 14:23:35Z, 92% at 14:29:52Z, seven_day resets 11-15 00:00Z.

    94.3% of the week had elapsed, so used-vs-elapsed called it behind pace; at +0.8%/min it hit 100% by 14:40Z."""
    reset = 1794700800.0  # 2026-11-15T00:00:00Z
    now, before = 1794666592.0, 1794666215.0  # 14:29:52Z, 14:23:35Z
    elapsed_fraction = 1 - (reset - now) / lp.WINDOW_SECONDS[lp.SEVEN_DAY]
    assert 92 / 100 < elapsed_fraction  # a used-vs-elapsed rule would not throttle
    cfg = lp.PacingConfig(reserve={lp.FIVE_HOUR: 101.0, lp.SEVEN_DAY: 101.0}, min_rate_span=300)
    current = [lp.Reading(now, "askevepodcast", lp.SEVEN_DAY, 92, reset)]
    history = [lp.Reading(before, "askevepodcast", lp.SEVEN_DAY, 87, reset)]
    pace = lp.pool_pace("anthropic", current, history, cfg)
    assert pace.state == lp.PACED and pace.factor < 0.01


def test_five_hour_bucket_gates_as_well():
    now = 1_800_000_000.0
    # Weekly is fine; the five-hour bucket went 0 -> 44% in 10 minutes (14:20-14:30Z on 11-14).
    current = [_r("a", lp.SEVEN_DAY, 20, 5 * DAY, now), _r("a", lp.FIVE_HOUR, 44, 4.83 * HOUR, now)]
    history = [_r("a", lp.FIVE_HOUR, 0, 5 * HOUR, now - 600)]
    assert lp.pool_pace("anthropic", current, history, CFG).state == lp.PACED


def test_every_account_past_reserve_holds_until_earliest_reset():
    now = 1_800_000_000.0
    current = [_r("a", lp.SEVEN_DAY, 100, 2 * DAY, now),
               _r("b", lp.SEVEN_DAY, 86, 9 * HOUR, now), _r("b", lp.FIVE_HOUR, 81, 3 * HOUR, now)]
    pace = lp.pool_pace("anthropic", current, [], CFG)
    assert pace.state == lp.HELD
    assert pace.until == now + 9 * HOUR  # b frees once both its reserved windows reset
    pool = lp.plan_pool("anthropic", lp.hashed(current), lp.Tpp(None), lp.Tpp(None), now, CFG)[0]
    assert pool["at_reserve"] is True and pool["until"] == pace.until  # the v2 view the start hold reads


@pytest.mark.parametrize("used", [(100, 40), (86, 84), (84, 86), (90, 95), (0, 85), (85, 85)])
@pytest.mark.parametrize("five_hour", [None, 50, 81])
def test_v2_at_reserve_is_exactly_the_legacy_held_verdict(used, five_hour):
    """Removing the start limit must not move a held pool: v2 ``at_reserve`` holds exactly when ``pool_pace`` said
    ``held``, with the same until."""
    now = 1_800_000_000.0
    current = [_r("a", lp.SEVEN_DAY, used[0], 2 * DAY, now), _r("b", lp.SEVEN_DAY, used[1], 3 * DAY, now)]
    if five_hour is not None:
        current.append(_r("b", lp.FIVE_HOUR, five_hour, 2 * HOUR, now))
    pace = lp.pool_pace("anthropic", current, [], CFG)
    pool = lp.plan_pool("anthropic", lp.hashed(current), lp.Tpp(1.0), lp.Tpp(1.0), now, CFG)[0]
    assert pool["at_reserve"] is (pace.state == lp.HELD)
    assert pool["until"] == (pace.until if pace.state == lp.HELD else None)


def test_one_account_under_reserve_is_not_held():
    now = 1_800_000_000.0
    current = [_r("a", lp.SEVEN_DAY, 100, 2 * DAY, now), _r("b", lp.SEVEN_DAY, 40, 6 * DAY, now)]
    assert lp.pool_pace("anthropic", current, [], CFG).state != lp.HELD


def test_no_readable_account_fails_open():
    pace = lp.pool_pace("anthropic", [], [], CFG)
    assert pace.state == lp.UNKNOWN
    pool = lp.plan_pool("anthropic", [], lp.Tpp(1.0), lp.Tpp(1.0), 1_800_000_000.0, CFG)[0]
    assert pool["at_reserve"] is False and pool["rate_tokens_per_s"] is None


def test_capped_window_burns_nothing_and_a_fresh_burst_counts_at_once():
    now = 1_800_000_000.0
    capped = _r("a", lp.SEVEN_DAY, 100, 2 * DAY, now)
    assert lp.burn_rate(capped, [], CFG) == 0.0
    current = _r("b", lp.FIVE_HOUR, 40, 4 * HOUR, now)
    history = [_r("b", lp.FIVE_HOUR, 10, 4 * HOUR + 3600, now - 3600),
               _r("b", lp.FIVE_HOUR, 20, 4 * HOUR + 600, now - 600)]
    assert lp.burn_rate(current, history, CFG) == pytest.approx(20 / 600)  # newest slope, not the hour's 30/3600


def test_config_from_reads_overrides_and_keeps_defaults():
    cfg = lp.config_from({"enabled": False, "target_percent": 90, "reserve_seven_day_percent": "x"})
    assert (cfg.enabled, cfg.target, cfg.reserve[lp.SEVEN_DAY]) == (False, 90.0, 85.0)
    assert lp.config_from(None) == lp.PacingConfig()


def test_window_kind_maps_anthropic_and_codex_labels():
    assert [lp.window_kind(x) for x in ("Current session", "Current week", "Session", "Weekly")] == [
        lp.FIVE_HOUR, lp.SEVEN_DAY, lp.FIVE_HOUR, lp.SEVEN_DAY]


# --- dispatch admission ------------------------------------------------------------------------------------

@pytest.fixture
def paced_board(board, monkeypatch):
    monkeypatch.setattr(pd, "_CACHE", {})
    monkeypatch.setattr(pd, "_CLASS_CACHE", {})
    return board


def _usage(monkeypatch, used, reset_in, *, kind=lp.SEVEN_DAY):
    """Every governed pool reads one account; with no history the burn is the window's average so far."""
    def read(provider, now):
        return [lp.Reading(now, f"{provider}-1", kind, used, now + reset_in)]
    monkeypatch.setattr(pd, "read_pool", read)


def _dispatch(conn):
    seen = []
    result = kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda task, ws: seen.append(task.id))
    return seen, result


def _opus_card(conn, title="work", **kw):
    mandatory = kw.pop("pin_mandatory", False)
    tid = kb.create_task(conn, title=title, assignee="default", model_override="class:standard", **kw)
    if mandatory:
        # Historical mark, seeded only in the isolated board fixture.
        conn.execute("UPDATE tasks SET model_override=?, provider_override='anthropic', model_pin_mandatory=1 WHERE id=?", (OPUS, tid))
        conn.commit()
    return tid


def test_pool_ahead_of_pace_starts_cards_by_default(paced_board, monkeypatch):
    """The default: a burn rate never holds a start. Slots decide concurrency; a token throttle the token rate."""
    _write_cfg(paced_board)
    _usage(monkeypatch, 70, 6 * DAY)  # 70% in the week's first day: paced
    with kbc.connect() as conn:
        a, b = _opus_card(conn, "a"), _opus_card(conn, "b")
        seen, result = _dispatch(conn)
        events = [e for t in (a, b) for e in kb.list_events(conn, t) if e.kind == pd.EVENT_KIND]
        stored = pd.pace_for(conn, "anthropic", pd.load_config(), time.time())
    assert sorted(seen) == sorted([a, b]) and not result.respawn_guarded and events == []
    assert stored.state == lp.PACED  # still judged and stored, for the panel and the runway


def test_held_pool_still_stops_starts_by_default(paced_board, monkeypatch):
    _write_cfg(paced_board)
    _usage(monkeypatch, 90, 6 * DAY)  # past the weekly reserve: no account has headroom
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        seen, result = _dispatch(conn)
    assert seen == [] and (tid, lp.GUARD_REASON) in result.respawn_guarded


def test_pool_behind_pace_does_not_defer(paced_board, monkeypatch):
    _write_cfg(paced_board)
    _usage(monkeypatch, 5, 6 * DAY)  # 5% after one day: ~35% at reset
    with kbc.connect() as conn:
        a, b = _opus_card(conn, "a"), _opus_card(conn, "b")
        seen, _ = _dispatch(conn)
    assert sorted(seen) == sorted([a, b])


def test_held_pool_names_the_reset_and_repeats_no_event(paced_board, monkeypatch):
    _write_cfg(paced_board)
    _usage(monkeypatch, 90, 9 * HOUR)
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        _dispatch(conn)
        _, result = _dispatch(conn)
        events = [e.payload for e in kb.list_events(conn, tid) if e.kind == pd.EVENT_KIND]
    assert len(events) == 1 and events[0]["providers"][0]["state"] == lp.HELD
    assert abs(result.clock_held[tid][1] - (time.time() + 9 * HOUR)) < 120


@pytest.mark.parametrize("persisted", [True, False], ids=["shared-state", "memory-only"])
def test_dispatch_reuses_usage_until_read_interval_expires(paced_board, monkeypatch, persisted):
    # Keep the synthetic account home distinct from HERMES_HOME: the inherited
    # board now carries a real creating chat, so consent reads its isolated state DB.
    account_home = paced_board.parent / "account-home"
    account_home.mkdir()
    monkeypatch.setenv("HOME", str(account_home))
    monkeypatch.setattr(pd, "_CLASS_CACHE", {})
    monkeypatch.setattr(pd, "_WARNED", {})
    interval = 60
    _write_cfg(paced_board, llm_pacing={"enabled": True, "read_interval_seconds": interval,
                                      "providers": ["anthropic"]})
    assert pd.load_config().read_interval == interval
    start = time.time()
    clock = SimpleNamespace(now=start)
    monkeypatch.setattr(pd, "time", SimpleNamespace(time=lambda: clock.now, monotonic=lambda: clock.now))
    path = paced_board / ("shared" if persisted else "unavailable") / "llm_pacing.json"
    if persisted:
        path.parent.mkdir()
    # A failed shared-state save must not defeat the in-process read budget.
    monkeypatch.setattr(pd, "state_path", lambda: path)
    reads = []

    def read(provider, now):
        reads.append((provider, now))
        used = 90 if len(reads) == 1 else 5
        return [_r("account", lp.SEVEN_DAY, used, 6 * DAY, now)]

    monkeypatch.setattr(pd, "read_pool", read)
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        for offset in (0, 1, interval - 1):
            clock.now = start + offset
            seen, result = _dispatch(conn)
            assert reads == [("anthropic", start)]
            assert seen == []
            assert (tid, lp.GUARD_REASON) in result.respawn_guarded
        assert path.exists() is persisted
        # Expiry is measured from the producer read, not the latest cache hit.
        clock.now = start + interval
        seen, _ = _dispatch(conn)
        assert reads == [("anthropic", start), ("anthropic", start + interval)]
        assert seen == [tid]


def test_governor_off_is_todays_behaviour(paced_board, monkeypatch):
    _write_cfg(paced_board, llm_pacing={"enabled": False})
    _usage(monkeypatch, 99, 9 * HOUR)
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        seen, _ = _dispatch(conn)
    assert seen == [tid]


def test_legacy_mandatory_pin_cannot_bypass_reserve_hold(paced_board, monkeypatch):
    _write_cfg(paced_board)
    _usage(monkeypatch, 99, 9 * HOUR)
    with kbc.connect() as conn:
        tid = _opus_card(conn, pin_mandatory=True)
        seen, result = _dispatch(conn)
    assert seen == []
    assert (tid, lp.GUARD_REASON) in result.respawn_guarded


def test_usage_read_failure_starts_the_card(paced_board, monkeypatch):
    _write_cfg(paced_board)

    def boom(provider, now):
        raise OSError("usage endpoint down")
    monkeypatch.setattr(pd, "read_pool", boom)
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        seen, _ = _dispatch(conn)
    assert seen == [tid]


def test_ungoverned_provider_is_not_throttled(paced_board, monkeypatch):
    _write_cfg(paced_board, llm_pacing={"providers": ["openai-codex"]})
    _usage(monkeypatch, 99, 9 * HOUR)
    with kbc.connect() as conn:
        tid = _opus_card(conn)
        seen, _ = _dispatch(conn)
    assert seen == [tid]
