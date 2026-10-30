"""A turn refused for a retryable reason is replayed through the crash path, not left on "send it again". [daveey]"""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

from .test_auto_continue import _InlineThread, _session

LEASE = "Chat ownership lease is unavailable"


@pytest.fixture()
def emits(monkeypatch):
    captured: list = []
    monkeypatch.setattr(server, "_emit", lambda event, sid, payload=None: captured.append((event, sid, payload)))
    return captured


@pytest.fixture()
def marker_home(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    return tmp_path


@pytest.fixture()
def turn_env(monkeypatch, tmp_path, marker_home):
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})


class _Timers(list):
    """Records the wait instead of running it."""

    def __call__(self, delay, fn):
        self.append((delay, fn))
        return types.SimpleNamespace(start=lambda: None, daemon=False)


@pytest.fixture()
def timers(monkeypatch):
    fake = _Timers()
    monkeypatch.setattr(threading, "Timer", fake)
    monkeypatch.setattr(server, "_REFUSED_RETRY_DELAYS_S", (45.0, 180.0))
    monkeypatch.setattr(server, "_active_goal_manager", lambda session: None)
    monkeypatch.setattr(server, "_auto_continue_config", lambda: (True, 900.0, 2))
    return fake


def _refused(message, **kwargs):
    return {"failed": True, "error": LEASE, "final_response": None}


def _agent(run=_refused):
    return types.SimpleNamespace(session_id="session-key", run_conversation=run, clear_interrupt=lambda: None)


def test_retryable_refusal_keeps_the_marker_and_waits(emits, turn_env, marker_home, timers):
    session = _session(agent=_agent(), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive my receipts")
    marker = read_turn_marker(marker_home, "session-key")
    assert marker["prompt"] == "archive my receipts" and marker["attempts"] == 0
    assert [t[0] for t in timers] == [45.0]


def test_non_retryable_failure_clears_the_marker_and_never_waits(emits, turn_env, marker_home, timers):
    def bad_model(message, **kwargs):
        return {"failed": True, "error": "HTTP 400: claude-opus-5-5 is not a valid model ID", "final_response": None,
                "failure_reason": "invalid_request", "failure_retryable": False}

    session = _session(agent=_agent(bad_model), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive my receipts")
    assert read_turn_marker(marker_home, "session-key") is None and not timers


def test_a_replay_waits_longer_and_the_last_one_gives_up(emits, turn_env, marker_home, timers):
    session = _session(agent=_agent(), running=True, _auto_continue_attempt=1, _auto_continue_prompt="p")
    server._run_prompt_submit("rid", "sid", session, server._auto_continue_note("p"), display_kind="auto_continue")
    assert read_turn_marker(marker_home, "session-key")["attempts"] == 1
    assert [t[0] for t in timers] == [180.0]

    timers.clear()
    session = _session(agent=_agent(), running=True, _auto_continue_attempt=2, _auto_continue_prompt="p")
    server._run_prompt_submit("rid", "sid", session, server._auto_continue_note("p"), display_kind="auto_continue")
    assert read_turn_marker(marker_home, "session-key") is None and not timers


@pytest.mark.parametrize("kind", ["process_complete", "kanban_notice"])
def test_automation_turns_are_not_replayed(emits, turn_env, marker_home, timers, kind):
    session = _session(agent=_agent(), running=True)
    server._run_prompt_submit("rid", "sid", session, "a delivery", display_kind=kind)
    assert read_turn_marker(marker_home, "session-key") is None and not timers


def test_a_goal_keeps_its_own_retry(emits, turn_env, marker_home, timers, monkeypatch):
    monkeypatch.setattr(server, "_active_goal_manager", lambda session: object())
    session = _session(agent=_agent(), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive my receipts")
    assert read_turn_marker(marker_home, "session-key") is None and not timers


def test_a_second_refused_message_joins_the_first(emits, turn_env, marker_home, timers):
    session = _session(agent=_agent(), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive waymo")
    session["running"] = True
    server._run_prompt_submit("rid", "sid", session, "and mercury")
    assert read_turn_marker(marker_home, "session-key")["prompt"] == "archive waymo\n\nand mercury"


def test_a_new_message_that_works_ends_the_replay(emits, turn_env, marker_home, timers, monkeypatch):
    calls = []
    results = iter([_refused("x"), {"final_response": "done"}])
    session = _session(agent=_agent(lambda message, **kw: next(results)), running=True)
    server._run_prompt_submit("rid", "sid", session, "archive waymo")
    session["running"] = True
    server._run_prompt_submit("rid", "sid", session, "something else")
    assert read_turn_marker(marker_home, "session-key") is None
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: calls.append(a))
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: None)
    monkeypatch.setattr("tui_gateway.turn_outcome.refresh_turn_outcome", lambda *a: None)
    timers[0][1]()
    assert len(calls) == 1  # it looks, and the guard finds no marker


def _armed(marker_home, timers, monkeypatch):
    session = _session(agent=_agent(), running=False, _refused_turn_delay=45.0)
    record_turn_start(marker_home, "session-key", "archive waymo")
    server._schedule_refused_turn_retry("sid", session, "session-key")
    return session, timers[-1][1]


def test_the_wait_replays_through_auto_continue(emits, turn_env, marker_home, timers, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: seen.append(a) or {"attempt": 1})
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: None)
    session, fire = _armed(marker_home, timers, monkeypatch)
    fire()
    assert seen == [("sid", session, "session-key")]


def test_a_skewed_backend_leaves_the_marker_for_the_boot_sweep(emits, turn_env, marker_home, timers, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a: seen.append(a))
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: "skew")
    _, fire = _armed(marker_home, timers, monkeypatch)
    fire()
    assert not seen and read_turn_marker(marker_home, "session-key")["prompt"] == "archive waymo"


def test_a_marker_that_aged_out_settles_the_outcome(emits, turn_env, marker_home, timers, monkeypatch):
    refreshed = []
    monkeypatch.setattr("tui_gateway.update_pause._detect_skew", lambda: None)
    monkeypatch.setattr("tui_gateway.turn_outcome.refresh_turn_outcome", lambda *a: refreshed.append(a))
    _, fire = _armed(marker_home, timers, monkeypatch)

    def aged_out(sid, sess, key):
        server.clear_turn_marker(marker_home, key)

    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", aged_out)
    fire()
    assert len(refreshed) == 1
