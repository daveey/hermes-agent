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
_LONG_PARK_S = 25 * 60.0  # one failed window + gap + most of the next: longer than the freshness window


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
def test_long_park_then_drainer_exit_continues_in_place(schedule_env, home, drainer, wall, monkeypatch):
    """(a) Parked past the freshness window, then the drainer exits: the watcher continues the turn."""
    import fcntl
    from tests.tui_gateway.test_update_pause import _InlineThread
    session = _capped_pause_under_drainer(home, monkeypatch)
    monkeypatch.setattr(up.threading, "Thread", _InlineThread)
    parked_from = wall.now()
    sleep = up.time.sleep

    def tick(seconds):
        sleep(seconds)
        if wall.now() - parked_from >= _LONG_PARK_S and not drainer.closed:
            assert read_turn_marker(home, "session-key")["reason"] == up.PAUSE_REASON
            assert schedule_env == []  # parked the whole time, never restarted
            fcntl.flock(drainer, fcntl.LOCK_UN)  # drainer gave up / hit its hard max
            drainer.close()

    up.time.sleep = tick
    up.start_in_place_watcher("sid", session, "session-key")
    assert drainer.closed, "watcher returned before the drainer exited"
    (text, kwargs), = schedule_env
    assert text == up.pause_note("original work")
    assert kwargs["display_kind"] == "auto_continue"
    assert session["_auto_continue_attempt"] == 0  # a pause, not a crash
    assert read_turn_marker(home, "session-key") is not None  # the continuation owns it now


@pytest.mark.platforms("posix")
def test_long_park_then_update_applies_startup_sweep_resumes(home, drainer, wall, monkeypatch):
    """(b) Parked past the freshness window, then the update applies: the next backend resumes it."""
    import json
    from tui_gateway import startup_resume as sr
    session = _capped_pause_under_drainer(home, monkeypatch)
    for _ in range(int(_LONG_PARK_S // up.WATCH_POLL_S)):  # the watcher's polls while parked
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
