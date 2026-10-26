"""Stale stream cancellation has one outcome regardless of socket unwind mode."""

import httpx
import pytest

import run_agent
from agent import chat_completion_helpers as helpers


@pytest.mark.parametrize(
    "ending,cancelled,interrupted",
    [
        ("eof", True, False),
        ("reset", True, False),
        ("protocol", True, False),
        ("reset", False, False),
        ("validation", True, False),
        ("reset", True, True),
    ],
)
def test_cancelled_stream_transport_outcome(monkeypatch, ending, cancelled, interrupted):
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    agent = run_agent.AIAgent(
        api_key="test-key", base_url="http://127.0.0.1:1/v1", model="m", provider="custom",
        quiet_mode=True, skip_context_files=True, skip_memory=True, enabled_toolsets=[], max_iterations=1,
    )
    call = helpers._StreamingCall(agent, {"model": "m", "messages": [{"role": "user", "content": "hi"}]}, None)
    call._stream_stale_timeout = 5.0
    call.worker = None
    call._monitor_interrupted = {"yes": False}
    original = {
        "reset": httpx.ReadError("connection reset by peer"),
        "protocol": httpx.RemoteProtocolError("incomplete chunked read"),
        "validation": ValueError("invalid provider payload"),
        "eof": None,
    }[ending]
    closed = []

    def stream():
        try:
            # Drive the real cancellation transition at a deterministic read boundary;
            # no watchdog timing or OS-dependent socket shutdown outcome is involved.
            if cancelled:
                call._kill_stale_stream(5.0)
            if interrupted:
                agent._interrupt_requested = True
                call._abort_for_interrupt(5.0)
            if original is not None:
                raise original
            yield from ()
        finally:
            closed.append(True)

    monkeypatch.setattr(call, "_open_chat_stream", lambda _kwargs: stream())
    call._call()

    assert closed == [True]
    assert call.result["response"] is None
    error = call.result["error"]
    if interrupted:
        # The monitor owns the InterruptedError; the worker must not report a drop.
        assert call._monitor_interrupted["yes"]
        assert error is None
    elif cancelled and ending != "validation":
        assert type(error) is httpx.RemoteProtocolError
        assert str(error) == "stream attempt 1 was superseded"
        if original is not None:
            assert error.__cause__ is original
    else:
        assert error is original
