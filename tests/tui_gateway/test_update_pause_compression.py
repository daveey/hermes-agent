"""[daveey] t_01a1d0aa52ee1404: a core-update pause no longer waits out a long context compression.

hermes-hub 2026-11-12 16:27:26-16:32:30: a Desktop turn's post-tool compression streamed its summary for 302 s.
The turn held its admission reservation unpublished the whole time (a pause lands only at a step boundary and
the mid-tool cancel tracked tool calls only), so the instant drainer was refused ~30 times and applied 306 s
after the update was due. The compression host wait now registers with ``agent.update_pause_cancel``: after
the same ``MID_TOOL_GRACE_S`` it fence-cancels the summary before its commit and returns the unchanged
transcript (no stall fallback, no timeout cooldown), and the turn pauses at the next step boundary, releasing
its reservation. A commit already admitted always finishes.

These drive a real ``AIAgent`` turn through the real tool round, the real ``_compress_context`` facade and its
progress-aware host wait, the real admission fence and the gateway's real hook and watcher; only the model, the
tool body and the summary engine (``compress_context``, a summary that keeps streaming) are fakes.
"""

from __future__ import annotations

import tempfile
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import agent.conversation_compression as cc
from agent import update_pause_cancel
from agent.conversation_compression import (
    CompressionCommitFence, context_compression_timed_out, run_compress_context_with_progress_timeout,
)
from hermes_cli import admission_fence as af
from hermes_state import SessionDB
from run_agent import AIAgent
from tui_gateway import server
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import read_turn_marker, record_turn_start

CFG = {"desktop": {"auto_continue": {"enabled": True, "resume_on_startup": True, "freshness_minutes": 240,
                                     "max_attempts": 2}}}
SUMMARY_CAP_S = 8.0  # a summary left alone streams this long, then commits
_TICK = threading.Event()  # never set: ``_TICK.wait(s)`` paces a fake stream without a wall-clock sleep


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
    agent.compression_enabled = True
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


class _StreamingSummary:
    """``compress_context`` whose summary keeps streaming (touching the fence) until cancelled or its cap."""

    def __init__(self, cap_s: float = SUMMARY_CAP_S):
        self.started = threading.Event()
        self.outcome = None
        self.cap_s = cap_s

    def __call__(self, agent, messages, system_message, *, commit_fence=None, **_kw):
        self.started.set()
        deadline = time.monotonic() + self.cap_s
        while time.monotonic() < deadline:
            if commit_fence is not None and commit_fence.is_cancelled:
                self.outcome = "cancelled"
                return messages, system_message
            if commit_fence is not None:
                commit_fence.touch_progress()
            _TICK.wait(0.02)
        self.outcome = "committed"
        return [{"role": "user", "content": "[summary]"}], "compressed prompt"


def _run_turn(agent, session, responses):
    """The gateway's per-turn wiring (prompt_turn._invoke_agent): hook + watcher around run_conversation."""
    agent.client.chat.completions.create.side_effect = responses
    hook = up.boundary_pause_hook(session, "session-key")
    agent.step_boundary_pause = hook
    stop = up.start_mid_tool_cancel(session, "session-key", agent)
    try:
        return agent.run_conversation("run the quick job")
    finally:
        stop()
        agent.step_boundary_pause = None


def test_a_long_compression_yields_to_the_update_pause_and_the_turn_pauses(home, tmp_path, monkeypatch):
    record_turn_start(home, "session-key", "run the quick job")
    agent = _make_agent(tmp_path)
    session = {"agent": agent, "session_key": "session-key", "profile_home": str(home),
               "history_lock": threading.RLock()}
    summary = _StreamingSummary()
    monkeypatch.setattr(cc, "compress_context", summary)
    monkeypatch.setattr(cc, "resolve_context_compression_timeouts", lambda *a, **k: (60.0, 600.0))
    monkeypatch.setattr(agent.context_compressor, "should_compress", lambda *a, **k: True)
    monkeypatch.setattr("model_tools.handle_function_call", lambda name, *a, **k: "quick result")

    # The turn's admission reservation (as the Desktop backend takes it), then the drainer's hold + pause.
    reservation = af.reserve(home)
    assert reservation is not None
    af.set_hold(home)
    af.request_pause(home, expires_at=time.time() + 120)
    released = {}

    def _turn():
        try:
            released["result"] = _run_turn(agent, session, [_response(_call("call_1")), _response(), _response()])
        finally:
            reservation.close()  # the backend releases the reservation at the turn's end
            released["at"] = time.monotonic()

    started = time.monotonic()
    thread = threading.Thread(target=_turn, daemon=True)
    thread.start()
    thread.join(SUMMARY_CAP_S * 3)
    assert not thread.is_alive()
    result = released["result"]

    assert summary.started.is_set()
    assert released["at"] - started < SUMMARY_CAP_S / 2  # the drainer sees no reservation long before the summary ends
    assert af.state(home)["reservations"] == 0
    assert summary.outcome == "cancelled"  # cancelled before its commit
    assert result["interrupted"] is True and result["paused_for_update"] == up.PAUSE_REASON
    assert agent.client.chat.completions.create.call_count == 1  # no model call after the yield
    marker = read_turn_marker(home, "session-key")
    assert marker["reason"] == "core_update_pause" and marker["pauses"] == 1
    assert not context_compression_timed_out(agent)  # a yield is not a stall: no timeout cooldown
    assert not agent._interrupt_requested
    assert update_pause_cancel.in_flight(agent) == {}


def _host_wait(fence, yield_check, *, commit=False):
    """The real host wait over a worker that streams until cancelled (or commits at once)."""
    release = threading.Event()

    def worker(worker_fence):
        if commit:
            assert worker_fence.begin_commit()
            try:
                release.wait(2)
                return [{"role": "user", "content": "[summary]"}], "compressed"
            finally:
                worker_fence.finish_commit()
        deadline = time.monotonic() + SUMMARY_CAP_S
        while time.monotonic() < deadline and not worker_fence.is_cancelled:
            worker_fence.touch_progress()
            _TICK.wait(0.02)
        return [], "late"

    def _release_soon():
        _TICK.wait(0.3)
        release.set()

    threading.Thread(target=_release_soon, daemon=True).start()
    messages = [{"role": "user", "content": "hi"}]
    started = time.monotonic()
    out = run_compress_context_with_progress_timeout(
        worker=worker, messages=messages, system_prompt_fallback="fallback", idle_timeout_seconds=60.0,
        total_ceiling_seconds=600.0, fence=fence, yield_check=yield_check,
        on_timeout=lambda *a: pytest.fail("a yield must not take the timeout path"),
    )
    return out, messages, time.monotonic() - started


def test_host_wait_yields_unchanged_without_the_stall_ladder():
    fence = CompressionCommitFence()
    asked = threading.Event()
    out, messages, took = _host_wait(fence, lambda: asked.set() or True)
    assert asked.is_set() and took < SUMMARY_CAP_S / 2
    assert out == (messages, "fallback") and out[0] is messages
    assert fence.is_cancelled


def test_host_wait_lets_an_admitted_commit_finish_on_a_yield():
    fence = CompressionCommitFence()
    out, _messages, _took = _host_wait(fence, lambda: fence.commit_in_flight, commit=True)
    assert out == ([{"role": "user", "content": "[summary]"}], "compressed")


def test_compression_counts_as_in_flight_only_while_it_runs():
    agent = types.SimpleNamespace(_interrupt_requested=False)
    agent.interrupt = lambda **kw: setattr(agent, "_interrupt_requested", True) or setattr(
        agent, "_tool_interrupt_reason", kw.get("tool_reason"))
    with update_pause_cancel.compression_in_flight(agent):
        assert list(update_pause_cancel.in_flight(agent)) == [update_pause_cancel.COMPRESSION_CALL_ID]
        assert not update_pause_cancel.compression_yield_requested(agent)
        assert update_pause_cancel.cancel(agent, "stamp") == [update_pause_cancel.COMPRESSION_CALL_ID]
        assert update_pause_cancel.compression_yield_requested(agent)
    assert update_pause_cancel.in_flight(agent) == {}
    assert update_pause_cancel.take_pending(agent) is True


def test_a_user_stop_is_not_a_compression_yield():
    agent = types.SimpleNamespace(_interrupt_requested=True, _tool_interrupt_reason="user interrupt")
    with update_pause_cancel.compression_in_flight(agent):
        assert update_pause_cancel.cancel(agent, "stamp") is None
        assert not update_pause_cancel.compression_yield_requested(agent)


class _OverflowError(Exception):
    """A provider 400 that proves the request is over the window (no reported limit)."""
    status_code = 400
    code = "context_length_exceeded"


def _overflow_error():
    return _OverflowError("This model's maximum context length is exceeded (context_length_exceeded)")


def _pause_once_summary_streams(home, summary):
    """The drainer's hold + pause, issued while the overflow recovery's summary streams."""
    def _issue():
        if summary.started.wait(5):
            af.set_hold(home)
            af.request_pause(home, expires_at=time.time() + 120)
    threading.Thread(target=_issue, daemon=True).start()


@pytest.mark.parametrize("responses", [
    pytest.param(lambda: [_response(_call("call_1")), _overflow_error(), _response(), _response()],
                 id="overflow-after-a-tool-round"),
    pytest.param(lambda: [_overflow_error(), _response(), _response()], id="overflow-on-the-first-model-call"),
])
def test_an_overflow_recovery_compression_never_yields(home, tmp_path, monkeypatch, responses):
    """Review 5878929471 on #1617: a yield is only safe where the pause can land (a tool-result boundary after a
    model call). An overflow recovery treats the unchanged transcript as exhaustion (the turn failed with
    ``compression_exhausted``, which auto-resets a gateway session) or re-sends the over-window request
    unpaused. Overflow recovery therefore never registers with the watcher: its summary commits."""
    record_turn_start(home, "session-key", "run the quick job")
    agent = _make_agent(tmp_path)
    session = {"agent": agent, "session_key": "session-key", "profile_home": str(home),
               "history_lock": threading.RLock()}
    summary = _StreamingSummary(cap_s=1.5)
    monkeypatch.setattr(cc, "compress_context", summary)
    monkeypatch.setattr(cc, "resolve_context_compression_timeouts", lambda *a, **k: (60.0, 600.0))
    monkeypatch.setattr(agent.context_compressor, "should_compress", lambda *a, **k: False)
    monkeypatch.setattr("model_tools.handle_function_call", lambda name, *a, **k: "quick result")
    _pause_once_summary_streams(home, summary)

    result = _run_turn(agent, session, responses())

    assert summary.started.is_set()
    assert summary.outcome == "committed"  # never cancelled by the update pause
    assert not result.get("failed") and not result.get("compression_exhausted")
    assert update_pause_cancel.in_flight(agent) == {}


@pytest.mark.parametrize("trigger", ["post_tool", "pre_api"])
def test_scripted_tool_tail_before_any_api_call_cannot_yield(tmp_path, monkeypatch, trigger):
    agent = _make_agent(tmp_path)
    agent._api_call_count = 0
    observed = []

    def summary(_agent, messages, system_message, **kwargs):
        observed.append(update_pause_cancel.in_flight(agent))
        return messages, system_message

    monkeypatch.setattr(cc, "compress_context", summary)
    agent._compress_context(
        [{"role": "tool", "tool_call_id": "prelude", "content": "opening"}],
        "system", trigger=trigger,
    )
    assert observed == [{}]


def _release_pause_at_boundary(home, streaming, monkeypatch):
    original = up.boundary_pause_hook

    def factory(*args):
        hook = original(*args)

        def released(**kwargs):
            if streaming.outcome == "cancelled":
                af.clear_pause_request(home)
                af.clear_hold(home)
            return hook(**kwargs)

        return released

    monkeypatch.setattr(up, "boundary_pause_hook", factory)


@pytest.mark.parametrize("decline_pause", [False, True])
def test_forced_overflow_preflight_yields_without_spending_attempt(home, tmp_path, monkeypatch, decline_pause):
    import agent.turn_preflight as preflight

    record_turn_start(home, "session-key", "run the quick job")
    agent = _make_agent(tmp_path)
    session = {"agent": agent, "session_key": "session-key", "profile_home": str(home),
               "history_lock": threading.RLock()}
    streaming = _StreamingSummary()
    summaries, attempts = [], []

    def summary(worker_agent, messages, system_message, **kwargs):
        if not summaries:
            assert kwargs["trigger"] == "overflow"
            summaries.append("committed")
            return [dict(m, content="short") if m.get("role") == "tool" else m for m in messages], system_message
        assert kwargs["trigger"] == "pre_api"
        result = streaming(worker_agent, messages, system_message, **kwargs)
        summaries.append(streaming.outcome)
        return result

    original = preflight.run_preflight_compression

    def capture(*args, **kwargs):
        before = args[1].compression_attempts
        result = original(*args, **kwargs)
        if streaming.started.is_set():
            attempts.append((before, result.compression_attempts))
        return result

    monkeypatch.setattr("agent.turn_preflight_gate.run_preflight_compression", capture)
    monkeypatch.setattr(cc, "compress_context", summary)
    monkeypatch.setattr(agent.context_compressor, "should_compress", lambda *a, **k: summaries == ["committed"])
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: "large result " * 10000)
    if decline_pause:
        _release_pause_at_boundary(home, streaming, monkeypatch)
    _pause_once_summary_streams(home, streaming)
    result = _run_turn(agent, session, [_response(_call("call_1")), _overflow_error(), _response()])
    assert summaries == ["committed", "cancelled"]
    assert not result.get("failed") and not result.get("compression_exhausted")
    if decline_pause:
        assert result["final_response"] == "done"
        assert attempts and all(before == after for before, after in attempts)
    else:
        assert result["paused_for_update"] == up.PAUSE_REASON


def test_declined_post_tool_pause_refunds_attempt(home, tmp_path, monkeypatch):
    import agent.turn_preflight as preflight

    record_turn_start(home, "session-key", "run the quick job")
    agent = _make_agent(tmp_path)
    session = {"agent": agent, "session_key": "session-key", "profile_home": str(home),
               "history_lock": threading.RLock()}
    streaming = _StreamingSummary()
    attempts = []
    original = preflight.compress_after_tool_results

    def capture(*args, **kwargs):
        result = original(*args, **kwargs)
        attempts.append((kwargs["compression_attempts"], result.compression_attempts))
        return result

    monkeypatch.setattr("agent.turn_tool_round.compress_after_tool_results", capture)
    monkeypatch.setattr(cc, "compress_context", streaming)
    monkeypatch.setattr(agent.context_compressor, "should_compress", lambda *a, **k: not streaming.started.is_set())
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: "quick result")
    _release_pause_at_boundary(home, streaming, monkeypatch)
    _pause_once_summary_streams(home, streaming)
    result = _run_turn(agent, session, [_response(_call("call_1")), _response()])
    assert streaming.outcome == "cancelled"
    assert result["final_response"] == "done" and not result.get("failed")
    assert attempts == [(0, 0)]


def test_only_a_tool_result_boundary_compression_can_yield():
    tool_tail = [{"role": "user", "content": "go"}, {"role": "tool", "tool_call_id": "c", "content": "ok"}]
    user_tail = [{"role": "user", "content": "go"}]
    assert update_pause_cancel.compression_can_yield("post_tool", tool_tail, api_call_count=1)
    assert update_pause_cancel.compression_can_yield("pre_api", tool_tail, api_call_count=1)
    assert not update_pause_cancel.compression_can_yield("pre_api", user_tail, api_call_count=1)
    for trigger in ("overflow", "turn_start_threshold", "engine_preflight", "idle", "gateway_hygiene", None):
        assert not update_pause_cancel.compression_can_yield(trigger, tool_tail, api_call_count=1)
