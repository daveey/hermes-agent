"""A turn stopped by backend shutdown (SIGTERM/atexit) must keep its crash marker so desktop auto-continue
resumes it on the next boot; a turn the user stopped must still retire it.

Before this, ``_stop_turns_before_exit`` interrupted the turn cleanly, the turn's terminal frame ran
``_retire_turn_marker``, and a backend restart (``hermes-sync``, update, respawn) looked like a user Stop:
nothing auto-continued and the chat sat on "Operation interrupted: waiting for model response".
"""

from __future__ import annotations

import threading

import pytest

from tui_gateway import server
from tui_gateway.turn_marker import read_turn_marker, record_turn_start


@pytest.fixture
def running_session(tmp_path):
    registered: list[str] = []

    def _make(key: str) -> dict:
        session = {"agent": None, "session_key": key, "running": True, "profile_home": str(tmp_path),
                   "history_lock": threading.RLock()}
        record_turn_start(tmp_path, key, "do the thing")
        with server._sessions_lock:
            server._sessions[key] = session
        registered.append(key)
        return session

    yield _make
    with server._sessions_lock:
        for key in registered:
            server._sessions.pop(key, None)


def test_shutdown_interrupted_turn_keeps_marker(running_session, tmp_path):
    session = running_session("sess-exit")
    server._stop_turns_before_exit(budget_s=0)
    # The interrupted turn now reaches its terminal frame, which retires the marker on a normal stop.
    server._retire_turn_marker(session, "sess-exit")
    marker = read_turn_marker(tmp_path, "sess-exit")
    assert marker is not None and marker["prompt"] == "do the thing"


def test_user_stopped_turn_still_retires_marker(running_session, tmp_path):
    session = running_session("sess-user-stop")
    server._retire_turn_marker(session, "sess-user-stop")
    assert read_turn_marker(tmp_path, "sess-user-stop") is None


def test_idle_session_not_flagged_at_exit(running_session, tmp_path):
    session = running_session("sess-idle")
    session["running"] = False
    server._stop_turns_before_exit(budget_s=0)
    server._retire_turn_marker(session, "sess-idle")
    assert read_turn_marker(tmp_path, "sess-idle") is None
