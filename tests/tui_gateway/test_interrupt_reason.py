"""Every TUI/desktop interrupt producer records WHY it stopped the turn [daveey].

A turn cut short while waiting for the model saved the same bare
"Operation interrupted: waiting for model response (N s elapsed)." row whether the user pressed Stop,
the backend exited (hermes-sync update, quit), the WS-orphan reaper fired, or a lease takeover won.
Nobody could tell a slow first token from a lost turn, so the coach filed every "interrupted, then
resume" as an agent fault (self-heal agent-38bad5aced).

Each entry point now passes a fixed reason to ``agent.interrupt``; the interrupted row carries
``display_metadata.interrupt = {reason, waited_s, prompt_tokens}`` and agent.log gets one INFO line
``turn interrupted: reason=... waited=...s prompt_tokens=...``.
"""

from __future__ import annotations

import logging
import threading
import types
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent
from tui_gateway import server


def _tool_defs(*names):
    return [{"type": "function", "function": {"name": n, "description": n,
                                              "parameters": {"type": "object", "properties": {}}}}
            for n in names]


def _tool_call_response():
    tc = SimpleNamespace(id=f"call_{uuid.uuid4().hex[:8]}", type="function",
                         function=SimpleNamespace(name="web_search", arguments="{}"))
    msg = SimpleNamespace(content="", tool_calls=[tc], reasoning=None, reasoning_content=None,
                          reasoning_details=None, refusal=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="tool_calls")],
                           model="test/model", usage=None)


@pytest.fixture
def agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs("web_search")),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a.compression_enabled = False
    a.save_trajectories = False
    return a


def _run_with_slow_model(agent, monkeypatch, trigger):
    """Run a turn whose 2nd model call (after one tool round) hangs until aborted; ``trigger`` fires the real
    gateway interrupt entry point from another thread once the call is in flight. Returns the turn result."""
    calls, in_flight, release = [], threading.Event(), threading.Event()

    def provider(*_a, **kw):
        calls.append(kw)
        if len(calls) == 1:
            return _tool_call_response()
        in_flight.set()
        release.wait(10)
        raise OSError("fixture request stopped")

    rc = MagicMock()
    rc.chat.completions.create.side_effect = provider
    agent.client.chat.completions.create.side_effect = provider
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **kw: rc)
    monkeypatch.setattr(agent, "_abort_request_openai_client", lambda *a, **kw: release.set())
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda *a, **kw: None)

    out = {}

    def run():
        with (
            patch("model_tools.handle_function_call", return_value="search result"),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            out["result"] = agent.run_conversation("look it up")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        assert in_flight.wait(15), "slow model call never started"
        trigger()
        t.join(20)
    finally:
        release.set()
    assert not t.is_alive(), "turn did not settle after the interrupt"
    return out["result"]


def _session(sid, agent):
    session = {"agent": agent, "agent_ready": threading.Event(), "agent_error": None, "history": [],
               "history_lock": threading.RLock(), "running": True, "session_key": sid, "_run_thread": None}
    server._sessions[sid] = session
    return session


@pytest.fixture
def sid(request):
    sid = f"intreason-{request.node.name}"[:60]
    yield sid
    server._sessions.pop(sid, None)


def _interrupted_row(result):
    rows = [m for m in result["messages"] if m.get("role") == "assistant"
            and isinstance(m.get("display_metadata"), dict) and "interrupt" in m["display_metadata"]]
    assert rows, f"no interrupted row carries a reason: {[ (m.get('role'), m.get('display_metadata')) for m in result['messages']]}"
    return rows[-1]["display_metadata"]["interrupt"]


def _assert_recorded(result, caplog, reason):
    assert result["interrupted"] is True
    info = _interrupted_row(result)
    assert info["reason"] == reason
    assert isinstance(info["waited_s"], (int, float)) and info["waited_s"] >= 0
    assert isinstance(info["prompt_tokens"], int) and info["prompt_tokens"] > 0
    assert result.get("interrupt") == info
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("turn interrupted:")]
    assert lines and f"reason={reason} " in lines[-1] and "prompt_tokens=" in lines[-1], lines


def test_user_stop_records_user_stop(agent, sid, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(server, "_clear_pending", lambda *_a, **_k: None)
    _session(sid, agent)
    transport = types.SimpleNamespace(auth_identity=None, _peer="127.0.0.1", write=lambda obj: True)
    from tui_gateway.transport import bind_transport, reset_transport

    def stop():
        token = bind_transport(transport)
        try:
            server._methods["session.interrupt"](1, {"session_id": sid})
        finally:
            reset_transport(token)

    result = _run_with_slow_model(agent, monkeypatch, stop)
    _assert_recorded(result, caplog, "user_stop")
    # A human stop is not attributed to a system producer.
    assert "interrupt_issuer" not in result


def test_backend_exit_records_backend_exit(agent, sid, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(server, "_clear_pending", lambda *_a, **_k: None)
    _session(sid, agent)
    result = _run_with_slow_model(agent, monkeypatch, lambda: server._stop_turns_before_exit(budget_s=0))
    _assert_recorded(result, caplog, "backend_exit")
    assert result["interrupt_issuer"] == "backend_exit"


def test_ws_orphan_reaper_records_ws_orphan(agent, sid, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(server, "_clear_pending", lambda *_a, **_k: None)
    session = _session(sid, agent)
    result = _run_with_slow_model(
        agent, monkeypatch,
        lambda: server._interrupt_session_turn(sid, session, request_id=f"client-gone-{sid}", orphan=True))
    _assert_recorded(result, caplog, "ws_orphan")


def test_lease_takeover_records_lease_takeover(agent, sid, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(server, "_clear_pending", lambda *_a, **_k: None)
    session = _session(sid, agent)
    result = _run_with_slow_model(
        agent, monkeypatch,
        lambda: server._interrupt_session_turn(sid, session, request_id="lease-taken-over",
                                               reason="lease_takeover"))
    _assert_recorded(result, caplog, "lease_takeover")


def test_compute_host_interrupt_frame_carries_the_reason(monkeypatch):
    """An isolated (compute-host) turn is interrupted in the child: the reason rides on the frame."""
    from tui_gateway.host_supervisor import HostSupervisor

    sent = []
    sup = HostSupervisor.__new__(HostSupervisor)
    monkeypatch.setattr(sup, "start", lambda: None, raising=False)
    monkeypatch.setattr(sup, "_send_frame", lambda frame: sent.append(frame), raising=False)
    sup.interrupt("s1", request_id="r1", reason="backend_exit")
    assert sent == [{"type": "interrupt", "sid": "s1", "request_id": "r1", "reason": "backend_exit"}]

    seen = {}
    from tui_gateway import compute_host
    host = compute_host.ComputeHost.__new__(compute_host.ComputeHost)
    monkeypatch.setattr(host, "_reply", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(host, "_guarded", lambda frame, kind, body, **kw: body(
        types.SimpleNamespace(_sessions={"s1": {}}, _interrupt_session_turn=lambda s, sess, **k: seen.update(k)),
        frame["sid"], frame.get("request_id")), raising=False)
    host._handle_interrupt({"sid": "s1", "request_id": "r1", "reason": "backend_exit"})
    assert seen.get("reason") == "backend_exit"
