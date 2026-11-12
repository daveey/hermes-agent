"""A paused turn whose record its own (live) writer closed still continues within the window's bound.

hermes-hub 2026-11-12 (t_01a1d0e86b497ca4): two headless kanban-notice turns in the gateway paused for an update.
The detached-turn reaper closed their records 60 s later, the in-place watcher read the closed record as "the user
moved on" and stopped, and every reader skipped the marker because its writer (the gateway) was alive: both turns
waited 60-70 min, with no "window ended" line and no stuck WARNING, until the next gateway restart.
"""
import logging
import os
import time
import types

import pytest

from hermes_cli import admission_fence as af
from tui_gateway import server, startup_resume as sr, update_pause as up
from tui_gateway.turn_marker import mark_turn_paused, marker_writer_state, read_turn_marker, record_turn_start

from tests.tui_gateway import test_update_pause_recovery as base

recovery = base.recovery
CFG = base.CFG


class _Clock:
    def __init__(self, on_tick):
        self.now, self.on_tick = 0.0, on_tick

    def sleep(self, seconds):
        self.now += seconds
        self.on_tick(self.now)

    def monotonic(self):
        return self.now


def _reap(sid):
    """The detached-turn reaper's close (``detached_turn_cleanup._arm``): pop, then tear down."""
    popped = server._pop_session_by_id(sid)
    assert popped is not None
    server._teardown_popped_session(popped, end_reason="ws_orphan_reap")
    return popped


def _paused_here(home):
    # This process paused the turn: its writer is alive, as the gateway was on the hub.
    record_turn_start(home, "paused", "finish the request")
    mark_turn_paused(home, "paused", up.PAUSE_REASON)
    marker = read_turn_marker(home, "paused")
    assert marker["writer_pid"] == os.getpid() and marker_writer_state(marker) == "alive"


def _watch_closed(monkeypatch, recovery, *, close_at, clear_at, max_s, on_tick=None, resume=None):
    monkeypatch.setattr(up, "_detect_skew", lambda: None)
    monkeypatch.setattr(up, "WATCH_MAX_S", max_s)
    af.set_hold(recovery.home)  # the drain window is up while the turn pauses
    sid = base.mount()["session_id"]
    _paused_here(recovery.home)
    session = server._sessions[sid]
    state = {}
    if resume is not None:  # only the watcher's re-open goes through it, not the mount above
        monkeypatch.setitem(server._methods, "session.resume", resume(server._methods["session.resume"]))

    def tick(now):
        if now >= close_at and "closed" not in state:
            state["closed"] = _reap(sid)
            assert session.get("_closing") and session.get("_finalized")
        if now >= clear_at and "cleared" not in state:
            af.clear_hold(recovery.home)  # the window ends without an update
            state["cleared"] = now
        if on_tick is not None:
            on_tick(now)

    clock = _Clock(tick)
    monkeypatch.setattr(up, "time", types.SimpleNamespace(sleep=clock.sleep, monotonic=clock.monotonic,
                                                          time=time.time))
    watcher = up.start_in_place_watcher(sid, session, "paused")
    watcher.join(10)
    assert not watcher.is_alive()
    return sid, clock, state


def test_closed_record_of_a_live_writer_continues_when_the_window_ends(recovery, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="tui_gateway.update_pause")
    sid, clock, state = _watch_closed(monkeypatch, recovery, close_at=60.0, clear_at=150.0, max_s=up.PARK_MAX_S)
    base.assert_continued(recovery)
    # Within one poll of the window's end, well inside PARK_MAX_S, and on a fresh record (the old one is gone).
    assert clock.now <= state["cleared"] + up.WATCH_POLL_S
    assert sid not in server._sessions
    assert any(s.get("session_key") == "paused" for s in server._sessions.values())
    assert "window ended without an update; continuing session paused" in caplog.text


def test_closed_record_left_flagged_running_still_continues(recovery, monkeypatch):
    # A torn-down record keeps whatever flags it had: a stale ``running`` on it is no turn in this process.
    def reap_stale(sid, _reap=_reap):
        popped = _reap(sid)
        with popped["history_lock"]:
            popped["running"] = True
        return popped
    monkeypatch.setitem(globals(), "_reap", reap_stale)
    _watch_closed(monkeypatch, recovery, close_at=60.0, clear_at=150.0, max_s=up.PARK_MAX_S)
    base.assert_continued(recovery)


def test_readers_still_leave_a_live_writers_pause_alone(recovery, monkeypatch):
    # The precondition on the hub: the startup sweep of another backend skips the marker (writer alive), so only
    # the writer itself can continue it.
    _paused_here(recovery.home)
    rows = sr.pending_resumes(recovery.home, CFG)
    assert rows == []


def test_closed_record_whose_reopen_keeps_failing_warns_as_stuck(recovery, monkeypatch, caplog):
    # The window ended but the re-open keeps being refused: the watch stays up and says so at STUCK_WARN_S
    # (the hub logged nothing for 60-70 min).
    caplog.set_level(logging.INFO, logger="tui_gateway.update_pause")
    def refused(real):
        return lambda rid, params: {"id": rid, "error": {"code": 5000, "message": "busy"}}
    _watch_closed(monkeypatch, recovery, close_at=60.0, clear_at=150.0,
                  max_s=up.STUCK_WARN_S + 3 * up.WATCH_POLL_S, resume=refused)
    assert recovery.calls == []
    assert "has been paused for" in caplog.text and "continuation not yet admitted" in caplog.text


def test_closed_record_whose_pause_another_process_took_is_not_continued_here(recovery, monkeypatch):
    def foreign(now):
        if now == 90.0:  # e.g. a successor rewrote the pause as its own
            path = recovery.home / "desktop" / "interrupted_turns.json"
            import json
            entries = json.loads(path.read_text())
            entries["paused"]["writer_pid"] = 2 ** 22 - 3
            path.write_text(json.dumps(entries))
    _, clock, _ = _watch_closed(monkeypatch, recovery, close_at=60.0, clear_at=150.0, max_s=up.PARK_MAX_S,
                                on_tick=foreign)
    assert recovery.calls == []
    assert clock.now <= 150.0 + up.WATCH_POLL_S  # the watch ended at once, not at WATCH_MAX_S


@pytest.mark.parametrize("first", [
    {"error": {"code": 5000, "message": "busy"}},
    {"result": {"session_id": "x"}},  # re-opened, but no continuation scheduled yet (e.g. a slot backoff)
])
def test_closed_record_resume_refused_once_is_retried(recovery, monkeypatch, first):
    calls = []

    def flaky(real):
        def handler(rid, params):
            calls.append(params)
            if len(calls) == 1:
                return {"id": rid, **first}
            return real(rid, params)
        return handler
    _, clock, state = _watch_closed(monkeypatch, recovery, close_at=60.0, clear_at=150.0, max_s=up.PARK_MAX_S,
                                    resume=flaky)
    base.assert_continued(recovery)
    assert len(calls) == 2 and all(p["source_from_store"] for p in calls)
    assert clock.now <= state["cleared"] + 2 * up.WATCH_POLL_S


def test_closed_record_in_no_store_ends_the_watch(recovery, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="tui_gateway.update_pause")
    def missing(real):
        return lambda rid, params: {"id": rid, "error": {"code": 4007, "message": "not found"}}
    _, clock, state = _watch_closed(monkeypatch, recovery, close_at=60.0, clear_at=150.0, max_s=up.PARK_MAX_S,
                                    resume=missing)
    assert recovery.calls == []
    assert clock.now <= state["cleared"] + up.WATCH_POLL_S
    assert "is in no store" in caplog.text


@pytest.mark.parametrize("flag", ["_closing", "_finalized"])
def test_closed_flags_count_as_closed(flag):
    import threading
    session = {"history_lock": threading.Lock(), flag: True}
    assert up.session_closed(session)
    assert not up.session_closed({"history_lock": threading.Lock()})
