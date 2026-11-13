"""[daveey] t_01a1d293248f5e12: a core-update pause no longer waits out a long model request.

hermes-hub 2026-11-13 01:22:19-01:25:08: a Desktop turn (20261111_200009_edbe39) sat in a 169 s model request
right after its tool results. The drainer asked it to pause at 01:22:21, but a pause lands only at a step
boundary and the mid-tool cancel covered tool calls and compressions, not the model request. The turn kept its
admission reservation unpublished the whole time, so every instant apply after the 60 s cap was refused
("1 unpublished reservation(s)") and the update applied 274 s after it was due.

A model request issued at a step boundary (the transcript ends on this turn's tool results) now registers with
``agent.update_pause_cancel``: after the same ``MID_TOOL_GRACE_S`` the watcher cancels it, its partial output is
discarded, and the turn pauses at that same boundary (resumable, reservation released). The turn's first
request (no tool result yet) is not a boundary and is left alone.

These drive a real ``AIAgent`` turn through the real retry loop, the real admission fence and the gateway's real
hook and watcher; only the model (a request that keeps running) and the tool body are fakes.
"""

from __future__ import annotations

import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent import update_pause_cancel
from hermes_cli import admission_fence as af
from hermes_state import SessionDB
from run_agent import AIAgent
from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 240,
                                     "max_attempts": 2}}}
SLOW_MODEL_S = 8.0  # a model request left alone runs this long, then answers
_TICK = threading.Event()  # never set: ``_TICK.wait(s)`` paces the fake request without a wall-clock sleep


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)


@pytest.fixture()
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: CFG)
    monkeypatch.setattr(up, "MID_TOOL_GRACE_S", 0.3)
    monkeypatch.setattr(up, "MID_TOOL_POLL_S", 0.05)
    return tmp_path


def _make_agent(tmp_path: Path) -> AIAgent:
    hermes_home = Path(tempfile.mkdtemp(prefix="hermes-test-home-", dir=tmp_path))
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    defs = [{"type": "function", "function": {"name": "quick_job", "description": "quick_job",
                                              "parameters": {"type": "object", "properties": {}}}}]
    with (
        patch("model_tools.get_tool_definitions", return_value=defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="session-key", source="tui", model="test/model")
    agent._session_db, agent._session_db_created, agent.session_id = db, True, "session-key"
    agent._last_flushed_db_idx, agent._flushed_db_message_ids = 0, set()
    agent._flushed_db_message_session_id, agent._persist_disabled = None, False
    return agent


def _response(*calls):
    message = types.SimpleNamespace(content=None if calls else "done", reasoning_content=None, reasoning=None,
                                    tool_calls=list(calls) or None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        message=message, finish_reason="tool_calls" if calls else "stop")], model="test/model", usage=None)


def _call(call_id):
    return types.SimpleNamespace(id=call_id, type="function",
                                 function=types.SimpleNamespace(name="quick_job", arguments="{}"))


class _Model:
    """The provider: scripted responses; the request numbered ``slow`` runs ``SLOW_MODEL_S`` and sets
    ``slow_started`` when it begins (the drainer's pause is issued then, mid-request)."""

    def __init__(self, responses, slow: int):
        self.responses = list(responses)
        self.slow = slow
        self.calls = 0
        self.slow_started = threading.Event()
        self.slow_finished = False

    def __call__(self, *a, **kw):
        self.calls += 1
        if self.calls == self.slow:
            self.slow_started.set()
            _TICK.wait(SLOW_MODEL_S)
            self.slow_finished = True
        return self.responses.pop(0)


def _pause_when(home, event):
    def _issue():
        if event.wait(5):
            af.set_hold(home)
            af.request_pause(home, expires_at=time.time() + 120)
    threading.Thread(target=_issue, daemon=True).start()


def _drive(home, tmp_path, monkeypatch, responses, slow):
    record_turn_start(home, "session-key", "run the quick job")
    agent = _make_agent(tmp_path)
    session = {"agent": agent, "session_key": "session-key", "profile_home": str(home),
               "history_lock": threading.RLock()}
    model = _Model(responses, slow)
    agent.client.chat.completions.create.side_effect = model
    monkeypatch.setattr("model_tools.handle_function_call", lambda name, *a, **k: "quick result")
    reservation = af.reserve(home)  # the turn's admission reservation, as the Desktop backend takes it
    assert reservation is not None
    _pause_when(home, model.slow_started)
    out = {}

    def _turn():
        agent.step_boundary_pause = up.boundary_pause_hook(session, "session-key")
        stop = up.start_mid_tool_cancel(session, "session-key", agent)
        try:
            out["result"] = agent.run_conversation("run the quick job")
        finally:
            stop()
            agent.step_boundary_pause = None
            reservation.close()  # the backend releases the reservation at the turn's end
            out["at"] = time.monotonic()

    started = time.monotonic()
    thread = threading.Thread(target=_turn, daemon=True)
    thread.start()
    thread.join(SLOW_MODEL_S * 3)
    assert not thread.is_alive()
    return agent, model, out["result"], out["at"] - started


def test_a_long_model_request_at_a_step_boundary_yields_and_the_turn_pauses(home, tmp_path, monkeypatch):
    agent, model, result, took = _drive(
        home, tmp_path, monkeypatch, [_response(_call("call_1")), _response(), _response()], slow=2)

    assert model.slow_started.is_set()
    assert took < SLOW_MODEL_S / 2  # the drainer sees the reservation gone long before the request would end
    assert af.state(home)["reservations"] == 0
    assert result["interrupted"] is True and result["paused_for_update"] == up.PAUSE_REASON
    assert model.calls == 2  # no further model call after the yield
    marker = read_turn_marker(home, "session-key")
    assert marker["reason"] == "core_update_pause" and marker["pauses"] == 1
    # The boundary is the tool result: the cancelled request left no assistant row (partial or stub) behind it.
    assert result["messages"][-1]["role"] == "tool"
    assert not agent._interrupt_requested
    assert update_pause_cancel.in_flight(agent) == {}


def test_the_first_model_request_is_not_a_boundary_and_is_left_alone(home, tmp_path, monkeypatch):
    _agent, model, result, took = _drive(home, tmp_path, monkeypatch, [_response()], slow=1)

    assert model.slow_finished  # nothing cancelled it: there is no tool result to pause after
    assert took >= SLOW_MODEL_S
    assert not result.get("paused_for_update")
    assert result["final_response"] == "done"


def test_model_request_counts_as_in_flight_only_at_a_boundary():
    agent = types.SimpleNamespace(_interrupt_requested=False)
    agent.interrupt = lambda **kw: setattr(agent, "_interrupt_requested", True) or setattr(
        agent, "_tool_interrupt_reason", kw.get("tool_reason"))
    at_boundary = [{"role": "user", "content": "go"}, {"role": "assistant", "content": None},
                   {"role": "tool", "content": "r"}]
    with update_pause_cancel.model_request_in_flight(agent, [{"role": "user", "content": "go"}], api_call_count=1):
        assert update_pause_cancel.in_flight(agent) == {}
    with update_pause_cancel.model_request_in_flight(agent, at_boundary, api_call_count=2):
        assert list(update_pause_cancel.in_flight(agent)) == [update_pause_cancel.MODEL_CALL_ID]
        assert not update_pause_cancel.model_yield_requested(agent)
        assert update_pause_cancel.cancel(agent, "stamp") == [update_pause_cancel.MODEL_CALL_ID]
        assert update_pause_cancel.model_yield_requested(agent)
        assert not update_pause_cancel.compression_yield_requested(agent)
    assert update_pause_cancel.in_flight(agent) == {}
    assert update_pause_cancel.take_pending(agent) is True


def test_a_user_stop_is_not_a_model_yield():
    agent = types.SimpleNamespace(_interrupt_requested=True, _tool_interrupt_reason="user interrupt")
    at_boundary = [{"role": "user", "content": "go"}, {"role": "tool", "content": "r"}]
    with update_pause_cancel.model_request_in_flight(agent, at_boundary, api_call_count=2):
        assert update_pause_cancel.cancel(agent, "stamp") is None
        assert not update_pause_cancel.model_yield_requested(agent)
