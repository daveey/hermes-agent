"""A host sleep must not count as turn idleness for the WS-orphan reaper (lid-close interrupts)."""

import types

from tui_gateway import server


def _session(agent):
    return {"agent": agent, "running": True}


def test_sleep_gap_in_wall_clock_does_not_make_turn_stale(monkeypatch):
    monkeypatch.setattr(server, "_WS_ORPHAN_ACTIVITY_STALE_S", 600.0)
    # Wall clock says 635s since the last activity (the Mac slept ~10 min), awake time says 5s.
    agent = types.SimpleNamespace(
        _last_activity_mono=1000.0,
        get_activity_summary=lambda: {"seconds_since_activity": 635.0},
    )
    monkeypatch.setattr(server.time, "monotonic", lambda: 1005.0)
    assert server._ws_orphan_turn_activity_is_fresh(_session(agent)) is True


def test_awake_idle_past_threshold_is_still_stale(monkeypatch):
    monkeypatch.setattr(server, "_WS_ORPHAN_ACTIVITY_STALE_S", 600.0)
    agent = types.SimpleNamespace(
        _last_activity_mono=1000.0,
        get_activity_summary=lambda: {"seconds_since_activity": 1.0},
    )
    monkeypatch.setattr(server.time, "monotonic", lambda: 1601.0)
    assert server._ws_orphan_turn_activity_is_fresh(_session(agent)) is False


def test_agent_without_mono_stamp_falls_back_to_summary(monkeypatch):
    monkeypatch.setattr(server, "_WS_ORPHAN_ACTIVITY_STALE_S", 600.0)
    agent = types.SimpleNamespace(get_activity_summary=lambda: {"seconds_since_activity": 700.0})
    assert server._ws_orphan_turn_activity_is_fresh(_session(agent)) is False


def test_touch_activity_stamps_monotonic(monkeypatch):
    from agent import activity_tracking

    touch = next(obj._touch_activity for obj in vars(activity_tracking).values()
                 if isinstance(obj, type) and hasattr(obj, "_touch_activity"))
    agent = types.SimpleNamespace(_persist_session_activity_if_due=lambda: None)
    monkeypatch.setattr(activity_tracking.time, "monotonic", lambda: 4242.0)
    touch(agent, "probe")
    assert agent._last_activity_mono == 4242.0
