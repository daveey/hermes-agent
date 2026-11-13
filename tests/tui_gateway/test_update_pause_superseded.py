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
    # This fixture runs turn threads inline, not process-lifetime ledger daemons.
    # Outcome derivation may read warm links; projection refresh is tested separately.
    from hermes_cli import kanban_session_activity as activity
    monkeypatch.setattr(activity, "_refresh_links_in_background", lambda scope: None)
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
    assert up.in_place_resume_action(session, "session-key", skew=lambda: None) == "carried"
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


# --- After a backend restart (the live 5ab65c shape): the paused marker was written by the old process, and the
# new backend's first turn on the session is a synthesized one. Whatever way that turn ends, the in-place watcher
# must be re-armed exactly once so the paused turn continues once the session is idle.

def _fresh_session_with_inherited_pause(home, run_conversation):
    from tui_gateway.turn_marker import mark_turn_paused, record_turn_start
    record_turn_start(home, "session-key", "the long task")
    assert mark_turn_paused(home, "session-key", up.PAUSE_REASON)
    session = {"session_key": "session-key", "history": [], "history_lock": threading.RLock(),
               "history_version": 0, "running": True, "attached_images": [], "image_counter": 0, "cols": 80,
               "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all", "inflight_turn": None,
               "profile_home": str(home)}
    session["agent"] = types.SimpleNamespace(session_id="session-key", run_conversation=run_conversation,
                                             clear_interrupt=lambda: None)
    return session


def _assert_paused_turn_continues(env, session):
    marker = read_turn_marker(env.home, "session-key")
    assert marker is not None and marker["reason"] == up.PAUSE_REASON
    assert marker["prompt"] == "the long task"
    assert env.watchers == [("ui-sid", "session-key")], "the paused turn has no watcher to continue it"
    assert not session.get("running")
    assert up.in_place_resume_action(session, "session-key", skew=lambda: None) == "resume"


def test_fresh_backend_notification_turn_success_rearms_the_watcher(env):
    session = _fresh_session_with_inherited_pause(
        env.home, lambda message, **kw: {"final_response": "noted", "messages": []})
    assert server._notif_run_submit("__notif__1", "ui-sid", session, "[Background process proc_1 finished]")
    _assert_paused_turn_continues(env, session)


def test_fresh_backend_notification_turn_that_raises_still_rearms_the_watcher(env):
    def _boom(message, **kw):
        raise RuntimeError("provider exploded")

    session = _fresh_session_with_inherited_pause(env.home, _boom)
    server._notif_run_submit("__notif__1", "ui-sid", session, "[Background process proc_1 finished]")
    _assert_paused_turn_continues(env, session)


def test_fresh_backend_notification_turn_refused_at_prepare_still_rearms_the_watcher(env, monkeypatch):
    def _never(message, **kw):
        raise AssertionError("a refused turn must not reach the agent")

    session = _fresh_session_with_inherited_pause(env.home, _never)
    monkeypatch.setattr(server, "_prepare_turn_input", lambda *a, **k: None)
    server._notif_run_submit("__notif__1", "ui-sid", session, "[Background process proc_1 finished]")
    _assert_paused_turn_continues(env, session)


# --- /goal continuation after a carried notice turn (t_01a18533d0a0182d). _run_post_turn_followups claimed the
# session directly for the goal continuation (no _notif_claim_turn gate) and did not mark it synthesized, so it
# wrote its own marker over the paused one and retired it: the paused request was replaced, not continued.

def _goal_after(monkeypatch, prompts):
    """The goal judge asks for a continuation after every turn whose prompt is in ``prompts``."""
    def _fake(sid, session, result, status, raw):
        return "[goal] keep going" if session.get("_test_last_prompt") in prompts else None
    real_submit = server._run_prompt_submit

    def _spy(rid, sid, session, text, *a, **kw):
        session["_test_last_prompt"] = text
        session.setdefault("_test_prompts", []).append(text)
        return real_submit(rid, sid, session, text, *a, **kw)

    monkeypatch.setattr(server, "_goal_followup_after_turn", _fake)
    monkeypatch.setattr(server, "_run_prompt_submit", _spy)


def test_goal_continuation_after_a_carried_notice_turn_keeps_the_paused_turn(env, monkeypatch):
    session = _paused_session(env.home)
    notice = "[Background process proc_1 finished]"
    _goal_after(monkeypatch, {notice})

    session["running"] = True  # the notification poller claimed the idle session
    assert server._notif_run_submit("__notif__1", "ui-sid", session, notice)

    assert session["_test_prompts"] == [notice], "the goal continuation ran ahead of the paused turn"
    marker = read_turn_marker(env.home, "session-key")
    assert marker is not None, "the goal continuation replaced the paused request"
    assert marker["reason"] == up.PAUSE_REASON and marker["prompt"] == "the long task"
    assert not session.get("running")
    assert up.in_place_resume_action(session, "session-key", skew=lambda: None) == "resume"


def test_goal_continuation_waits_while_a_paused_turn_continuation_is_due(env, monkeypatch):
    session = _paused_session(env.home)
    _goal_after(monkeypatch, {"some turn"})
    monkeypatch.setattr(up, "continuation_due", lambda s: True)
    session.pop("_update_pause_marker_key", None)
    server._run_post_turn_followups("rid", "ui-sid", session, {}, "[goal] keep going")
    assert "_test_prompts" not in session and not session.get("running")


def test_goal_continuation_still_runs_once_the_pause_is_retired(env, monkeypatch):
    session = _paused_session(env.home)
    typed = "actually, do something else"
    _goal_after(monkeypatch, {typed})

    session["running"] = True
    server._run_prompt_submit("rid-2", "ui-sid", session, typed)  # the user moved on: pause retired

    assert session["_test_prompts"] == [typed, "[goal] keep going"]
    assert read_turn_marker(env.home, "session-key") is None


# --- A synthesized turn running over the paused marker must itself pause when the next drain window asks.
# Live, hermes-hub 2026-11-13 (t_01a1d3083b2dae09): a process_complete turn landed on paused session 8d8534 between
# two drain windows (03:44:20). With no marker of its own it got no step-boundary hook, so it ran 12 minutes through
# the next windows; hermes-sync's drainer skips core_update_pause markers, so it never waited on it either, and its
# admission reservation (no gateway publishes Desktop turns) refused every instant apply for over an hour.

def _carried_turn_with_window(env, during_turn):
    """Pause a turn, then run a notification turn over the paused marker that opens a new drain window
    mid-turn and returns whatever ``during_turn(agent)`` says the turn ended with."""
    session = _paused_session(env.home)
    seen = {}

    def _run(message, **kwargs):
        af.set_hold(env.home)
        af.request_pause(env.home, expires_at=time.time() + 120)
        try:
            return during_turn(session["agent"], seen)
        finally:
            af.clear_pause_request(env.home)
            af.clear_hold(env.home)

    session["agent"].run_conversation = _run
    session["running"] = True
    assert server._notif_run_submit("__notif__1", "ui-sid", session, "[Background process proc_1 finished]")
    return session, seen


def test_synthesized_turn_over_a_paused_marker_pauses_at_its_next_step_boundary(env):
    def _turn(agent, seen):
        hook = agent.step_boundary_pause
        seen["hook"] = hook
        reason = hook() if callable(hook) else None
        seen["reason"] = reason
        return {"final_response": "", "messages": [], "interrupted": bool(reason), "completed": not reason,
                "paused_for_update": reason}

    session, seen = _carried_turn_with_window(env, _turn)

    assert callable(seen["hook"]), "a synthesized turn over a paused marker has no step-boundary pause hook"
    assert seen["reason"] == up.PAUSE_REASON
    marker = read_turn_marker(env.home, "session-key")
    # The paused request is kept as it was: same prompt, still core_update_pause, not counted as a second pause.
    assert marker is not None and marker["reason"] == up.PAUSE_REASON
    assert marker["prompt"] == "the long task"
    assert marker["pauses"] == 1 and marker["attempts"] == 0
    # The carried turn ended and re-armed the watcher; the paused turn continues once the window is over.
    assert env.watchers[-1] == ("ui-sid", "session-key")
    assert not session.get("running")
    assert up.in_place_resume_action(session, "session-key", skew=lambda: None) == "resume"


def test_synthesized_turn_over_a_paused_marker_keeps_running_without_a_pause_request(env):
    session = _paused_session(env.home)
    seen = {}

    def _run(message, **kwargs):
        seen["reason"] = session["agent"].step_boundary_pause()
        return {"final_response": "noted", "messages": []}

    session["agent"].run_conversation = _run
    session["running"] = True
    assert server._notif_run_submit("__notif__1", "ui-sid", session, "[Background process proc_1 finished]")
    assert seen["reason"] is None


def test_synthesized_turn_whose_carried_pause_was_retired_meanwhile_does_not_pause(env):
    from tui_gateway.turn_marker import clear_turn_marker

    def _turn(agent, seen):
        clear_turn_marker(env.home, "session-key")  # e.g. the user's Stop retired the paused request
        seen["reason"] = agent.step_boundary_pause()
        return {"final_response": "noted", "messages": []}

    _session, seen = _carried_turn_with_window(env, _turn)
    assert seen["reason"] is None


def test_synthesized_turn_over_a_paused_marker_arms_the_mid_tool_cancel_and_clarify_withdrawal(env, monkeypatch):
    # A long tool call (the live turn sat 193 s in one kanban_create) must be cancellable too, or the turn
    # reaches no step boundary before the drainer gives the window up.
    armed, stopped, clarify = [], [], []
    monkeypatch.setattr(up, "start_mid_tool_cancel",
                        lambda session, key, agent: armed.append(key) or (lambda: stopped.append(key)))
    monkeypatch.setattr(up, "install_clarify_pause_provider", lambda: clarify.append(True))
    session = _paused_session(env.home)
    for seen in (armed, stopped, clarify):  # the paused turn's own
        seen.clear()

    session["agent"].run_conversation = lambda message, **kw: {"final_response": "noted", "messages": []}
    session["running"] = True
    assert server._notif_run_submit("__notif__1", "ui-sid", session, "[Background process proc_1 finished]")
    assert armed == ["session-key"] and stopped == ["session-key"] and clarify == [True]
