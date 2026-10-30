"""A spent pause budget must drain, not veto the checkpoint or loop between windows."""
from __future__ import annotations

import time

import pytest

from hermes_cli import admission_fence as af
from tui_gateway import server, update_pause as up
from tui_gateway.turn_marker import mark_turn_paused, read_turn_marker, record_turn_start
from tests.tui_gateway.test_update_pause import _session

pytest_plugins = ["tests.tui_gateway.test_update_pause"]


@pytest.fixture()
def drainer(home):
    import fcntl
    path = home / "cache" / "core-update" / "drain.lock"
    path.parent.mkdir(parents=True)
    with path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield lock


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("pauses", [3, 8])
def test_spent_budget_drains_next_window_without_restart_loop(turn_env, home, drainer, monkeypatch, pauses):
    """A capped turn checkpoints and stays idle across failed windows.

    Admission and ordering proof only; the separate real-drainer integration harness
    exercises the window clock and apply callback.
    """
    skew: list = [None]
    monkeypatch.setattr(up, "_detect_skew", lambda: skew[0])
    calls = []
    session = _session(profile_home=str(home), running=True,
                       _auto_continue_pauses=pauses, _auto_continue_prompt="original work")

    def run(message, **kwargs):
        calls.append(message)
        assert af.state(home)["reservations"] == 1
        af.set_hold(home)
        af.request_pause(home, expires_at=time.time() + 900)
        return {"final_response": "", "messages": [], "interrupted": True, "completed": False,
                "paused_for_update": session["agent"].step_boundary_pause()}

    import types
    session["agent"] = types.SimpleNamespace(session_id="session-key", run_conversation=run,
                                             clear_interrupt=lambda: None)
    server._run_prompt_submit("r", "sid", session, up.pause_note("original work"))
    marker = read_turn_marker(home, "session-key")
    assert marker and marker["reason"] == up.PAUSE_REASON
    assert marker["pauses"] == pauses + 1 and marker["attempts"] == 0
    assert af.state(home)["reservations"] == 0
    assert session["running"] is False

    # Another gate prevented the first apply. Expiration is NOT evidence of progress.
    af.clear_pause_request(home)
    af.clear_hold(home)
    for _ in range(4):
        assert up.in_place_resume_action(session, "session-key") == "wait"
        assert server._maybe_schedule_auto_continue("sid", session, "session-key") is None
    assert len(calls) == 1
    parked = read_turn_marker(home, "session-key")
    assert parked.pop("parked_at") >= marker.get("paused_at", 0) and parked == marker

    # The next window sees the capped turn already idle; it cannot consume another window.
    af.set_hold(home)
    assert af.state(home) == {"version": 1, "held": True, "reservations": 0}
    skew[0] = ("old", "new")  # updater applies after the admission proof
    af.clear_hold(home)
    assert up.in_place_resume_action(session, "session-key") == "leave"
    assert read_turn_marker(home, "session-key")["prompt"] == "original work"


@pytest.mark.platforms("posix")
def test_spent_budget_recovers_when_drainer_exits(schedule_env, home, drainer, monkeypatch):
    import fcntl
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    record_turn_start(home, "session-key", "original work", pauses=up.MAX_PAUSES)
    mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    session = _session(profile_home=str(home))
    assert up.in_place_resume_action(session, "session-key") == "wait"
    assert server._maybe_schedule_auto_continue("sid", session, "session-key") is None
    assert schedule_env == []
    # flock is released on clean exit or process death; the leftover file is not a hold.
    fcntl.flock(drainer, fcntl.LOCK_UN)
    assert up.in_place_resume_action(session, "session-key") == "resume"
    assert server._maybe_schedule_auto_continue("sid", session, "session-key") is not None
    assert len(schedule_env) == 1
    assert session["_auto_continue_attempt"] == 0
    assert "original work" in schedule_env[0][0]


@pytest.mark.platforms("posix")
def test_foreign_root_keeps_the_requesting_drainers_lock(home, tmp_path, monkeypatch):
    import fcntl
    import json
    root = tmp_path / "other-install"
    root.mkdir()
    lock_path = home / "cache" / "core-update" / "drain.lock"
    lock_path.parent.mkdir(parents=True)
    session = _session(profile_home=str(root))
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    record_turn_start(root, "session-key", "original work", pauses=up.MAX_PAUSES)
    af.set_hold(root)
    af.request_pause(root, expires_at=time.time() + 120)
    request = root / "core-update" / "pause-request"
    body = json.loads(request.read_text())
    body["drain_lock"] = str(lock_path)
    request.write_text(json.dumps(body))
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert up.boundary_pause_hook(session, "session-key")() == up.PAUSE_REASON
        af.clear_pause_request(root)
        af.clear_hold(root)
        assert not (root / "cache" / "core-update" / "drain.lock").exists()
        assert up.in_place_resume_action(session, "session-key") == "wait"
        assert server._maybe_schedule_auto_continue("sid", session, "session-key") is None
    assert up.in_place_resume_action(session, "session-key") == "resume"


@pytest.mark.platforms("posix")
def test_below_threshold_still_recovers_between_windows(home, drainer, monkeypatch):
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    record_turn_start(home, "session-key", "original work")
    mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    assert up.in_place_resume_action(_session(profile_home=str(home)), "session-key") == "resume"


@pytest.mark.platforms("posix")
def test_capped_retry_keeps_notice_hold_bounded(home, drainer, monkeypatch):
    import types
    from tests.tui_gateway.test_update_pause import _InlineThread
    clock = [0.0]
    observations = []
    session = _session(profile_home=str(home), _kanban_pending=["queued owner notice"])
    record_turn_start(home, "session-key", "original work", pauses=up.MAX_PAUSES)
    mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    monkeypatch.setattr(up.threading, "Thread", _InlineThread)
    monkeypatch.setattr(up, "WATCH_MAX_S", up.HOLD_MAX_S + 60)
    def tick(seconds):
        clock[0] += seconds
        if clock[0] > up.HOLD_MAX_S + 20:
            observations.append(up.continuation_due(session))
    monkeypatch.setattr(up, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=tick, time=time.time))
    up.start_in_place_watcher("sid", session, "session-key")
    assert observations and not any(observations)
    assert session["_kanban_pending"] == ["queued owner notice"]
    assert read_turn_marker(home, "session-key")["prompt"] == "original work"


# ── Parking never outlives continuation eligibility (default 15-min freshness) ──────────────────────

DEFAULT_FRESHNESS_CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True,
                                                       "freshness_minutes": 15, "max_attempts": 2}}}


@pytest.fixture()
def wall(monkeypatch):
    """One clock for wall time (markers, freshness) and monotonic time (watcher, stamp throttle)."""
    import types
    clock = [time.time(), 0.0]

    def advance(seconds):
        clock[0] += seconds
        clock[1] += seconds

    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(up, "_now", lambda: clock[0])
    monkeypatch.setattr(up, "_mono", lambda: clock[1])
    monkeypatch.setattr(up, "time", types.SimpleNamespace(monotonic=lambda: clock[1], sleep=advance,
                                                          time=lambda: clock[0]))
    return types.SimpleNamespace(now=lambda: clock[0], advance=advance)


def _capped_pause_under_drainer(home, monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: DEFAULT_FRESHNESS_CFG)
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    session = _session(profile_home=str(home))
    record_turn_start(home, "session-key", "original work", pauses=up.MAX_PAUSES)
    af.set_hold(home)
    af.request_pause(home, expires_at=time.time() + 120)
    assert up.boundary_pause_hook(session, "session-key")() == up.PAUSE_REASON  # age 0 + margin < 15 min
    af.clear_pause_request(home)
    af.clear_hold(home)  # the window expired without applying
    return session


@pytest.mark.platforms("posix")
def test_park_is_bounded_while_the_drainer_still_runs(schedule_env, home, drainer, wall, monkeypatch):
    """(a) A capped pause parks only up to PARK_MAX_S, then continues in place although the drainer still
    runs (t_01a18b11cabcd2c2: parking for the drainer's life held two chats 60 min)."""
    from tests.tui_gateway.test_update_pause import _InlineThread
    session = _capped_pause_under_drainer(home, monkeypatch)
    monkeypatch.setattr(up.threading, "Thread", _InlineThread)
    parked_from = wall.now()
    up.start_in_place_watcher("sid", session, "session-key")
    assert not drainer.closed, "the drainer still holds its lock"
    assert up.PARK_MAX_S <= wall.now() - parked_from <= up.PARK_MAX_S + 2 * up.WATCH_POLL_S
    (text, kwargs), = schedule_env
    assert text == up.pause_note("original work")
    assert kwargs["display_kind"] == "auto_continue"
    assert session["_auto_continue_attempt"] == 0  # a pause, not a crash
    assert read_turn_marker(home, "session-key") is not None  # the continuation owns it now


@pytest.mark.platforms("posix")
def test_long_park_then_update_applies_startup_sweep_resumes(home, drainer, wall, monkeypatch):
    """(b) Parked (within PARK_MAX_S), then the update applies: the next backend resumes it."""
    import json
    from tui_gateway import startup_resume as sr
    session = _capped_pause_under_drainer(home, monkeypatch)
    for _ in range(int(up.PARK_MAX_S // up.WATCH_POLL_S) - 1):  # the watcher's polls while parked
        wall.advance(up.WATCH_POLL_S)
        assert up.in_place_resume_action(session, "session-key") == "wait"
    monkeypatch.setattr(up, "_detect_skew", lambda: ("old", "new"))  # the second window applied
    assert up.in_place_resume_action(session, "session-key") == "leave"
    path = home / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    entries["session-key"]["writer_pid"] = 2 ** 22 - 3  # the pre-update backend has exited
    entries["session-key"].pop("writer_start_time", None)
    path.write_text(json.dumps(entries))
    wall.advance(120.0)  # the new backend boots and runs its sweep
    assert [r["session_key"] for r in sr.pending_resumes(home, DEFAULT_FRESHNESS_CFG)] == ["session-key"]
    assert read_turn_marker(home, "session-key")["prompt"] == "original work"
    # The freshness bound still applies once nothing keeps the turn parked.
    wall.advance(15 * 60.0)
    assert sr.pending_resumes(home, DEFAULT_FRESHNESS_CFG) == []


@pytest.mark.platforms("posix")
def test_wall_clock_jump_while_parked_still_continues(schedule_env, home, drainer, monkeypatch):
    """A laptop asleep while parked: wall time jumps past the freshness window between two polls,
    and the drainer is gone at wake. The turn still continues (review probe, round 1)."""
    import fcntl
    session = _capped_pause_under_drainer(home, monkeypatch)
    assert up.in_place_resume_action(session, "session-key") == "wait"
    later = time.time() + 16 * 60
    monkeypatch.setattr(time, "time", lambda: later)
    monkeypatch.setattr(up, "_now", lambda: later)
    fcntl.flock(drainer, fcntl.LOCK_UN)
    assert up.in_place_resume_action(session, "session-key") == "resume"
    assert server._maybe_schedule_auto_continue("sid", session, "session-key") is not None
    (text, _), = schedule_env
    assert text == up.pause_note("original work")


# ── t_01a18b11cabcd2c2: a paused turn waits on the apply itself, nothing else ─────────────────────────


@pytest.mark.platforms("posix")
def test_first_window_miss_capped_pause_continues_before_15_minutes(schedule_env, home, drainer, wall, monkeypatch):
    """David-MBP 2026-10-29: two capped chats paused at 20:24 in a window that expired at 20:36:15; an apply
    attempt followed at once (20:36:16) and the drainer then lived until 21:22 waiting on a peer app build.
    They parked for the drainer's whole life and continued only at 21:25. Replay that sequence: window held
    until the pause is ~10 min old, a 10 s apply attempt retakes the hold right after expiry, then the gap
    with the drainer still alive. The turn must continue in place less than 15 min after its pause."""
    from tests.tui_gateway.test_update_pause import _InlineThread
    monkeypatch.setattr(server, "_load_cfg", lambda: DEFAULT_FRESHNESS_CFG)
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    monkeypatch.setattr(up.threading, "Thread", _InlineThread)
    monkeypatch.setattr(up, "WATCH_MAX_S", 40 * 60.0)
    session = _session(profile_home=str(home))
    record_turn_start(home, "session-key", "original work", pauses=up.MAX_PAUSES)
    af.set_hold(home)
    af.request_pause(home, expires_at=time.time() + 3600)
    assert up.boundary_pause_hook(session, "session-key")() == up.PAUSE_REASON
    paused_at = wall.now()
    window_end, attempt_end = paused_at + 600.0, paused_at + 610.0
    sleep, phase = up.time.sleep, ["window"]

    def tick(seconds):
        sleep(seconds)
        now = wall.now()
        if phase[0] == "window" and now >= window_end:  # expired: request cleared, then hold released
            af.clear_pause_request(home)
            phase[0] = "attempt"  # ...and the drainer's next apply attempt takes the hold at once
        elif phase[0] == "attempt" and now >= attempt_end:
            af.clear_hold(home)  # refused (no app build): the gap; the drainer lives on
            phase[0] = "gap"

    up.time.sleep = tick
    up.start_in_place_watcher("sid", session, "session-key")
    continued_after = wall.now() - paused_at
    assert schedule_env, "the paused turn was never continued"
    assert not drainer.closed, "continued while the drainer still runs"
    assert continued_after < 15 * 60.0, f"paused {continued_after:.0f}s"
    (text, _), = schedule_env
    assert text == up.pause_note("original work")


def test_stuck_pause_logs_a_warning_naming_session_and_blocker(home, caplog):
    """A turn paused past STUCK_WARN_S logs one WARNING with the session and what it waits on."""
    session = _session(profile_home=str(home))
    session["_update_pause_why"] = "update admission is held (drain window or update in progress)"
    with caplog.at_level("WARNING", logger=up.logger.name):
        assert up.warn_if_stuck(session, "session-key", up.STUCK_WARN_S - 1, False) is False
        assert not caplog.records
        assert up.warn_if_stuck(session, "session-key", up.STUCK_WARN_S + 1, False) is True
        assert up.warn_if_stuck(session, "session-key", up.STUCK_WARN_S + 60, True) is True
    (rec,) = caplog.records
    assert rec.levelname == "WARNING"
    assert "session-key" in rec.getMessage() and "update admission is held" in rec.getMessage()


@pytest.mark.platforms("posix")
def test_watcher_warns_when_a_hold_keeps_the_turn_paused(home, wall, monkeypatch, caplog):
    """End to end through the watcher: a hold that never ends keeps the turn paused; after STUCK_WARN_S the
    watcher logs the WARNING with the session and the hold as the blocker."""
    from tests.tui_gateway.test_update_pause import _InlineThread
    monkeypatch.setattr(server, "_load_cfg", lambda: DEFAULT_FRESHNESS_CFG)
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    monkeypatch.setattr(up.threading, "Thread", _InlineThread)
    monkeypatch.setattr(up, "WATCH_MAX_S", up.STUCK_WARN_S + 30)
    session = _session(profile_home=str(home))
    record_turn_start(home, "session-key", "original work")
    af.set_hold(home)
    af.request_pause(home, expires_at=time.time() + 3600)
    assert up.boundary_pause_hook(session, "session-key")() == up.PAUSE_REASON
    af.clear_pause_request(home)  # the hold stays (an update that never finishes)
    with caplog.at_level("WARNING", logger=up.logger.name):
        up.start_in_place_watcher("sid", session, "session-key")
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, warnings
    assert "session-key" in warnings[0] and "admission is held" in warnings[0]
