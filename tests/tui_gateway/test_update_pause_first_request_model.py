"""[daveey] t_01a1d31bf400d56c: a continuation's first model request yields to a core-update pause.

A continuation (kind ``auto_continue``) re-runs a paused request, so its first model request redoes nothing the
next backend's continuation would not redo. The mid-tool cancel reaches it after the same grace as a boundary
request (t_01a1d293248f5e12) and the turn pauses before any request, instead of after a whole first step
(hermes-hub 2026-11-13: 30 s first request + 21 s tool, 203 s after admission). Any other turn's first request
is still left alone (tests/tui_gateway/test_update_pause_model_request.py)."""

from __future__ import annotations

import threading
import time

import pytest

from hermes_cli import admission_fence as af
from tests.tui_gateway import test_update_pause_model_request as base
from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

NOTE = "[System note: Your previous turn was paused for a Hermes update ...]"


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: base.CFG)
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 0.3)
    monkeypatch.setattr(up, "MID_TOOL_POLL_S", 0.05)
    return tmp_path


def _drive_continuation(home, tmp_path, monkeypatch, responses):
    record_turn_start(home, "session-key", "run the quick job")
    agent = base._make_agent(tmp_path)
    session = {"agent": agent, "session_key": "session-key", "profile_home": str(home),
               "history_lock": threading.RLock()}
    model = base._Model(responses, 1)
    agent.client.chat.completions.create.side_effect = model
    reservation = af.reserve(home)
    assert reservation is not None
    base._pause_when(home, model.slow_started)
    out = {}

    def _turn():
        agent.step_boundary_pause = up.boundary_pause_hook(session, "session-key")
        agent.update_pause_first_request = True  # tui_gateway.prompt_turn marks an auto_continue turn
        stop = up.start_mid_tool_cancel(session, "session-key", agent)
        try:
            out["result"] = agent.run_conversation(NOTE, conversation_history=[
                {"role": "user", "content": "run the quick job"}])
        finally:
            stop()
            agent.step_boundary_pause = None
            reservation.close()
            out["at"] = time.monotonic()

    started = time.monotonic()
    thread = threading.Thread(target=_turn, daemon=True)
    thread.start()
    thread.join(base.SLOW_MODEL_S * 3)
    assert not thread.is_alive()
    return agent, model, out["result"], out["at"] - started


def test_a_continuations_first_request_yields_and_the_turn_pauses(home, tmp_path, monkeypatch):
    agent, model, result, took = _drive_continuation(
        home, tmp_path, monkeypatch, [base._response(), base._response()])

    assert model.slow_started.is_set() and not model.slow_finished
    assert took < base.SLOW_MODEL_S / 2
    assert af.state(home)["reservations"] == 0
    assert result["interrupted"] is True and result["paused_for_update"] == up.PAUSE_REASON
    assert model.calls == 1
    assert read_turn_marker(home, "session-key")["reason"] == "core_update_pause"
    # Nothing but user rows (alternation repair may merge the note into the prompt): no partial or interrupt row.
    assert {m["role"] for m in result["messages"]} == {"user"}
    assert not agent._interrupt_requested


def test_first_request_counts_as_in_flight_only_for_a_continuation():
    from agent import update_pause_cancel as upc
    first = [{"role": "user", "content": "go"}]
    assert upc.model_request_can_yield(first, api_call_count=1) is False
    assert upc.model_request_can_yield(first, api_call_count=1, first_request_ok=True) is True
    assert upc.model_request_can_yield([], api_call_count=1, first_request_ok=True) is False
