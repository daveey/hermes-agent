"""A progressed turn in API-error backoff remains a resumable update boundary."""
from __future__ import annotations

import threading
import time

import httpx
import pytest
from openai import RateLimitError

from agent import turn_api_error, update_pause_cancel
from hermes_cli import admission_fence as af
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start
from tests.tui_gateway import test_update_pause_model_request as helpers

home = helpers.home
_quiet = helpers._quiet


@pytest.mark.parametrize("mode", ["boundary", "first", "continuation", "stop", "opt_out"])
def test_api_error_backoff_pause_custody(home, tmp_path, monkeypatch, mode):
    agent = helpers._make_agent(tmp_path)
    first = mode in {"first", "continuation"}
    if mode == "continuation":
        agent.update_pause_first_request = True
    if mode == "opt_out":
        monkeypatch.setattr(helpers.server, "_load_cfg", lambda: {"desktop": {"auto_continue": {"enabled": False}}})
    record_turn_start(home, "session-key", "run the quick job")
    session = {"agent": agent, "session_key": "session-key", "profile_home": str(home),
               "history_lock": threading.Lock()}
    entered = threading.Event()
    issued = threading.Event()
    calls = []
    request = httpx.Request("POST", "https://example.com/chat")
    error = RateLimitError("rate limited", response=httpx.Response(429, request=request), body=None)

    def provider(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1 and not first:
            return helpers._response(helpers._call("call_1"))
        if len(calls) == (1 if first else 2):
            raise error
        return helpers._response()

    real_sleep = turn_api_error.interruptible_backoff_sleep

    def observed_sleep(*args, **kwargs):
        entered.set()
        assert issued.wait(5)
        return real_sleep(*args, **kwargs)

    def pause():
        if entered.wait(5):
            af.set_hold(home)
            af.request_pause(home, expires_at=time.time() + 120)
            issued.set()

    if mode == "stop":
        real_cancel = update_pause_cancel.cancel

        def stop_after_cancel(*args, **kwargs):
            cancelled = real_cancel(*args, **kwargs)
            if cancelled:
                agent.interrupt()
            return cancelled

        monkeypatch.setattr(update_pause_cancel, "cancel", stop_after_cancel)

    agent._interruptible_api_call = provider
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: "quick result")
    monkeypatch.setattr(turn_api_error, "compute_error_backoff", lambda *a, **k: 3.0)
    monkeypatch.setattr(turn_api_error, "interruptible_backoff_sleep", observed_sleep)
    reservation = af.reserve(home)
    assert reservation is not None
    producer = threading.Thread(target=pause)
    producer.start()
    agent.step_boundary_pause = up.boundary_pause_hook(session, "session-key")
    stop = up.start_mid_tool_cancel(session, "session-key", agent)
    try:
        result = agent.run_conversation("run the quick job")
    finally:
        stop()
        producer.join(6)
        reservation.close()
    assert not producer.is_alive()
    assert entered.is_set() and issued.is_set()
    if mode in {"first", "opt_out"}:
        assert not result.get("paused_for_update")
        assert result["final_response"] == "done"
        assert len(calls) == (2 if first else 3)
        return
    if mode == "stop":
        assert not result.get("paused_for_update")
        assert result["interrupted"] is True
        assert len(calls) == 2
        return
    assert result.get("paused_for_update") == up.PAUSE_REASON
    assert len(calls) == (1 if first else 2)
    assert result["messages"][-1]["role"] == ("user" if first else "tool")
    assert af.state(home)["reservations"] == 0
    assert (read_turn_marker(home, "session-key") or {}).get("reason") == up.PAUSE_REASON
    assert not agent._interrupt_requested
    assert update_pause_cancel.in_flight(agent) == {}
