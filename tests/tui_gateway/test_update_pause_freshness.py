"""[daveey] A cooperative core-update pause restarts the auto-continue freshness clock at ``paused_at``.

Crash recovery keeps the ``started_at`` clock ("don't resurrect stale turns"); a live turn that runs 80 minutes
and then accepts a pause is not 80 minutes stale. hermes-sync reads ``PAUSE_RESTARTS_FRESHNESS`` from this
module's text to decide whether its drain may ask such a turn to pause.
"""
from __future__ import annotations

import json
import time

import pytest

from tui_gateway import server, startup_resume as sr, update_pause as up
from tui_gateway.turn_marker import freshness_since, mark_turn_paused, read_turn_marker, record_turn_start
from tests.tui_gateway.test_update_pause import CFG, _session, _request_pause, home, schedule_env

AGE_S = 80 * 60.0
CFG15 = {"desktop": {"auto_continue": {**CFG["desktop"]["auto_continue"], "freshness_minutes": 15}}}


@pytest.fixture(autouse=True)
def _fifteen_minute_window(monkeypatch, home):  # the shipped default: an 80-minute-old turn is far outside it
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG15)


def _age(home, key="session-key", seconds=AGE_S):
    path = home / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    entries[key]["started_at"] = time.time() - seconds
    path.write_text(json.dumps(entries))


def _kill_writer(home, key="session-key"):
    path = home / "desktop" / "interrupted_turns.json"
    entries = json.loads(path.read_text())
    entries[key]["writer_pid"] = 2 ** 22 - 3
    entries[key].pop("writer_start_time", None)
    path.write_text(json.dumps(entries))


def test_turn_start_marker_advertises_that_its_pause_restarts_the_clock(home):
    record_turn_start(home, "session-key", "p")
    entries = json.loads((home / "desktop" / "interrupted_turns.json").read_text())
    assert entries["session-key"]["pause_restarts_freshness"] is True  # hermes-sync reads it per marker


def test_capability_constant_is_advertised():
    assert up.PAUSE_RESTARTS_FRESHNESS is True


def test_eighty_minute_old_live_turn_accepts_a_pause(home):
    record_turn_start(home, "session-key", "long work")
    _age(home)
    assert up.not_resumable_reason(_session(profile_home=str(home)), "session-key") is None
    _request_pause(home)
    assert up.boundary_pause_hook(_session(profile_home=str(home)), "session-key")() == up.PAUSE_REASON


def test_freshness_since_is_paused_at_after_the_pause(home):
    record_turn_start(home, "session-key", "long work")
    _age(home)
    mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    marker = read_turn_marker(home, "session-key")
    assert marker["paused_at"] > marker["started_at"] + 4000
    assert freshness_since(marker) == marker["paused_at"]
    marker["parked_at"] = marker["paused_at"] + 30
    assert freshness_since(marker) == marker["parked_at"]


def test_crash_interrupted_marker_keeps_the_started_at_clock(home):
    record_turn_start(home, "session-key", "long work")
    _age(home)
    marker = read_turn_marker(home, "session-key")
    assert freshness_since(marker) == marker["started_at"]


def test_startup_sweep_continues_an_old_paused_turn_but_not_an_old_crashed_one(home):
    record_turn_start(home, "session-key", "paused work")
    record_turn_start(home, "crashed-key", "crashed work")
    _age(home, "session-key")
    _age(home, "crashed-key")
    mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    _kill_writer(home, "session-key")
    _kill_writer(home, "crashed-key")
    assert [r["session_key"] for r in sr.pending_resumes(home, CFG15)] == ["session-key"]


def test_in_place_continue_resumes_an_old_paused_turn(home):
    record_turn_start(home, "session-key", "long work")
    _age(home)
    mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    session = _session(profile_home=str(home))
    assert up.in_place_resume_action(session, "session-key", skew=lambda: None) == "resume"


def test_scheduler_continues_an_old_paused_turn_and_clears_an_old_crashed_one(schedule_env, home):
    record_turn_start(home, "session-key", "long work")
    _age(home)
    mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    assert server._maybe_schedule_auto_continue("sid", _session(), "session-key") is not None
    record_turn_start(home, "crashed-key", "crashed work")
    _age(home, "crashed-key")
    assert server._maybe_schedule_auto_continue("sid", _session(), "crashed-key") is None


def test_max_pauses_still_caps(home):
    record_turn_start(home, "session-key", "long work")
    _age(home)
    for _ in range(up.MAX_PAUSES):
        mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    assert read_turn_marker(home, "session-key")["pauses"] == up.MAX_PAUSES
    # The cap is enforced by the drain/window logic (test_update_pause_cap.py); a capped pause is still
    # recorded and its clock counted from the last pause.
    marker = read_turn_marker(home, "session-key")
    assert freshness_since(marker) == marker["paused_at"]
