"""[daveey] A synthesized turn (background-process completion, kanban notice, subagent result) that lands on
a session whose turn is paused for a core update must not swallow the pause.

Live, David-MBP 2026-10-28: session 5ab65c was asked to pause at 07:43:15; after the update its next turn
was a process_complete notification (07:50:12), not an auto-continue. That turn wrote its own marker over
the ``core_update_pause`` one and cleared it when it finished, so neither the in-place watcher (session
busy -> "done") nor any startup sweep ever continued the paused request. A typed user message still
retires the pause: the user moved on.
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from hermes_cli import admission_fence as af
from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 240,
                                     "max_attempts": 2}}}


class _InlineThread:
    def __init__(self, target=None, daemon=None, args=(), kwargs=None, name=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    watchers: list = []
    monkeypatch.setattr(up, "start_in_place_watcher", lambda sid, session, key: watchers.append((sid, key)))
    return types.SimpleNamespace(home=tmp_path, watchers=watchers)


def _paused_session(home):
    """Run one turn that pauses at a step boundary, then end the window (hold and request gone)."""
    session = {"session_key": "session-key", "history": [], "history_lock": threading.RLock(),
               "history_version": 0, "running": True, "attached_images": [], "image_counter": 0, "cols": 80,
               "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
               "profile_home": str(home)}
    calls = {"n": 0}

    def _run(message, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            af.set_hold(home)
            af.request_pause(home, expires_at=time.time() + 120)
            return {"final_response": "", "messages": [], "interrupted": True, "completed": False,
                    "paused_for_update": session["agent"].step_boundary_pause()}
        return {"final_response": "noted", "messages": []}

    session["agent"] = types.SimpleNamespace(session_id="session-key", run_conversation=_run,
                                             clear_interrupt=lambda: None)
    server._run_prompt_submit("rid", "ui-sid", session, "the long task")
    af.clear_pause_request(home)
    af.clear_hold(home)
    marker = read_turn_marker(home, "session-key")
    assert marker is not None and marker["reason"] == up.PAUSE_REASON
    return session


def test_notification_turn_on_a_paused_session_keeps_the_paused_turn_continuable(env):
    session = _paused_session(env.home)

    session["running"] = True  # the notification poller claimed the idle session
    assert server._notif_run_submit("__notif__1", "ui-sid", session, "[Background process proc_1 finished]")

    marker = read_turn_marker(env.home, "session-key")
    assert marker is not None, "the paused request was dropped by the notification turn"
    assert marker["reason"] == up.PAUSE_REASON
    assert marker["prompt"] == "the long task"
    assert marker["pauses"] == 1 and marker["attempts"] == 0
    # Re-armed: the in-place watcher (pause turn's + this turn's) continues it once the session is idle.
    assert env.watchers == [("ui-sid", "session-key"), ("ui-sid", "session-key")]
    assert up.in_place_resume_action(session, "session-key", skew=lambda: None) == "resume"


def test_watcher_waits_while_a_synthesized_turn_runs_over_the_pause(env):
    session = _paused_session(env.home)
    session["running"] = True
    session["_paused_marker_carried"] = "session-key"
    assert up.in_place_resume_action(session, "session-key", skew=lambda: None) == "wait"
    session.pop("_paused_marker_carried")
    assert up.in_place_resume_action(session, "session-key", skew=lambda: None) == "done"


def test_typed_user_message_on_a_paused_session_still_retires_the_pause(env):
    session = _paused_session(env.home)

    session["running"] = True
    server._run_prompt_submit("rid-2", "ui-sid", session, "actually, do something else")

    assert read_turn_marker(env.home, "session-key") is None
    assert env.watchers == [("ui-sid", "session-key")]  # only the pause turn's own


def test_notification_turn_without_a_pause_leaves_no_marker(env):
    session = _paused_session(env.home)
    server._run_prompt_submit("rid-2", "ui-sid", session, "user moved on")  # pause retired
    assert read_turn_marker(env.home, "session-key") is None

    session["running"] = True
    assert server._notif_run_submit("__notif__2", "ui-sid", session, "[Background process proc_2 finished]")

    assert read_turn_marker(env.home, "session-key") is None
    assert env.watchers == [("ui-sid", "session-key")]
