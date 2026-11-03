"""Chat Completions expose the turn's per-provider-response context accounting as the optional
``usage.context_usage`` (``agent/context_usage.py``), streaming and non-streaming alike, on both the
direct adapter path (``_finish_turn_result``) and the canonical session-authority path
(``run_api_turn`` -> ``TurnRunner._run_conversation_with_approval`` -> committed admission usage).

Every fixture drives a real ``AIAgent.run_conversation`` (only the provider call is scripted), so the
measurements come from the agent's own accounting and reach the client through the real wire
serializers. The billing sums (prompt/completion/total) must stay exactly what they were.
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
pytest.importorskip("aiohttp.test_utils", reason="aiohttp not installed (optional 'messaging' extra)")
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tests.agent.test_turn_context_usage import (
    FALLBACK, PRIMARY, WINDOW, _agent, _entry, _fake_execute, _resp, _scripted, _tool_call,
)

TWO_CALLS = {"version": 1, "complete": True, "requests": [_entry(PRIMARY, 1_000), _entry(PRIMARY, 1_200)]}
# completion_tokens is 5 per scripted response (tests/agent/test_turn_context_usage._usage).
TWO_CALL_SUMS = {"prompt_tokens": 2_200, "completion_tokens": 10, "total_tokens": 2_210}


def _two_call_script():
    return _scripted(_resp("", prompt=1_000, finish="tool_calls", tool_calls=[_tool_call()]),
                     _resp("done", prompt=1_200))


@contextmanager
def _armed(agent, api_call, extra=()):
    """The agent with only its provider call scripted (same seams as the agent-side suite)."""
    with ExitStack() as stack:
        for cm in (
            patch.object(agent, "_interruptible_api_call", side_effect=api_call),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
            patch.object(agent, "_execute_tool_calls", side_effect=_fake_execute),
            patch("agent.turn_recovery.interruptible_backoff_sleep", return_value=None),
            patch("agent.agent_runtime_helpers.time.sleep"),
            *extra,
        ):
            stack.enter_context(cm)
        yield agent


def _billing(usage):
    return {k: v for k, v in usage.items() if k != "context_usage"}


async def _chat(app, *, stream, text="hello"):
    """POST /v1/chat/completions -> the wire ``usage`` block (the final chunk's when streaming).
    A distinct ``text`` is a distinct conversation (header-less session ids derive from it)."""
    body = {"model": "hermes-agent", "messages": [{"role": "user", "content": text}], "stream": stream}
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/v1/chat/completions", json=body)
        text = await response.text()
    assert response.status == 200, text
    if not stream:
        return json.loads(text)["usage"]
    chunks = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]
    usages = [chunk["usage"] for chunk in chunks if "usage" in chunk]
    assert len(usages) == 1, text  # only the finish chunk carries usage
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop", text
    return usages[0]


# ── direct adapter path (no session authority) ─────────────────────────────────────────────────


def _direct_app():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    assert getattr(adapter.gateway_runner, "session_authority", None) is None
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return adapter, app


async def _direct(agent, api_call, *, stream, extra=()):
    adapter, app = _direct_app()
    with _armed(agent, api_call, extra), patch.object(adapter, "_create_agent", return_value=agent):
        return await _chat(app, stream=stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_direct_two_measurements_keep_the_aggregate_sum(stream):
    agent = _agent()
    usage = await _direct(agent, _two_call_script(), stream=stream)
    assert usage["context_usage"] == TWO_CALLS
    # Billing is the same aggregate the wire always carried, equal to the agent's own counters.
    assert _billing(usage) == TWO_CALL_SUMS
    assert usage["prompt_tokens"] == agent.session_prompt_tokens == sum(
        r["input_tokens"] for r in usage["context_usage"]["requests"])


@pytest.mark.asyncio
async def test_direct_stream_and_nonstream_carry_identical_context_usage():
    nonstream = await _direct(_agent(), _two_call_script(), stream=False)
    streamed = await _direct(_agent(), _two_call_script(), stream=True)
    assert streamed == nonstream == {**TWO_CALL_SUMS, "context_usage": TWO_CALLS}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_direct_single_response(stream):
    usage = await _direct(_agent(), _scripted(_resp("ok", prompt=700)), stream=stream)
    assert usage == {"prompt_tokens": 700, "completion_tokens": 5, "total_tokens": 705,
                     "context_usage": {"version": 1, "complete": True, "requests": [_entry(PRIMARY, 700)]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_direct_response_without_usage_is_reported_incomplete_not_faked(stream):
    usage = await _direct(_agent(), _scripted(_resp("no usage")), stream=stream)
    assert usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                     "context_usage": {"version": 1, "complete": False, "requests": []}}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_direct_retry_lists_only_the_received_response(stream):
    class ReadTimeout(Exception):
        pass

    usage = await _direct(_agent(), _scripted(ReadTimeout("read timed out"), _resp("ok", prompt=300)), stream=stream)
    assert usage["context_usage"] == {"version": 1, "complete": True, "requests": [_entry(PRIMARY, 300)]}
    assert _billing(usage) == {"prompt_tokens": 300, "completion_tokens": 5, "total_tokens": 305}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_direct_fallback_switch_reports_the_runtime_that_answered(stream):
    class RateLimitError(Exception):
        status_code = 429

        def __init__(self):
            super().__init__("Error code: 429 - rate limit exceeded")
            self.response = SimpleNamespace(headers={})
            self.body = {"error": {"message": "rate limit exceeded"}}

    agent = _agent([{"provider": FALLBACK[0], "model": FALLBACK[1],
                     "base_url": "https://open.bigmodel.cn/api/coding/paas/v4"}])
    fb_client = MagicMock(api_key="primary-key-abcdef12", base_url="https://open.bigmodel.cn/api/coding/paas/v4",
                          _custom_headers=None, default_headers=None)
    usage = await _direct(agent, _scripted(RateLimitError(), _resp("via fallback", prompt=400)), stream=stream, extra=(
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(fb_client, FALLBACK[1])),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
        patch("agent.model_metadata.get_model_context_length", return_value=WINDOW),
    ))
    assert usage["context_usage"] == {"version": 1, "complete": True, "requests": [_entry(FALLBACK, 400)]}
    assert usage["prompt_tokens"] == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_direct_mid_turn_identity_change_is_per_response(stream):
    agent = _agent()

    def switch_then_answer(api_kwargs):
        agent.model = FALLBACK[1]
        return _resp("done", prompt=900)

    usage = await _direct(agent, _scripted(
        _resp("", prompt=800, finish="tool_calls", tool_calls=[_tool_call()]), switch_then_answer), stream=stream)
    assert usage["context_usage"]["requests"] == [_entry(PRIMARY, 800), _entry(FALLBACK, 900, None)]
    assert _billing(usage) == {"prompt_tokens": 1_700, "completion_tokens": 10, "total_tokens": 1_710}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_direct_concurrent_turns_are_isolated(stream):
    """Two overlapping requests on two agents: each response carries only its own measurements."""
    adapter, app = _direct_app()
    barrier = threading.Barrier(2, timeout=30)
    agents = [_agent(), _agent()]

    def script(first, second):
        calls = _scripted(_resp("", prompt=first, finish="tool_calls", tool_calls=[_tool_call()]),
                          _resp("done", prompt=second))

        def call(api_kwargs):
            barrier.wait()  # both turns are mid-flight before either records a response
            return calls(api_kwargs)
        return call

    pending = list(agents)
    with ExitStack() as stack:
        stack.enter_context(_armed(agents[0], script(111, 222)))
        stack.enter_context(patch.object(agents[1], "_interruptible_api_call", side_effect=script(333, 444)))
        for name in ("_persist_session", "_save_trajectory", "_cleanup_task_resources"):
            stack.enter_context(patch.object(agents[1], name))
        stack.enter_context(patch.object(agents[1], "_execute_tool_calls", side_effect=_fake_execute))
        stack.enter_context(patch.object(adapter, "_create_agent", side_effect=lambda **kw: pending.pop(0)))
        first, second = await asyncio.gather(_chat(app, stream=stream, text="first conversation"),
                                             _chat(app, stream=stream, text="second conversation"))
    got = sorted([first, second], key=lambda u: u["prompt_tokens"])
    assert got[0] == {"prompt_tokens": 333, "completion_tokens": 10, "total_tokens": 343,
                      "context_usage": {"version": 1, "complete": True,
                                        "requests": [_entry(PRIMARY, 111), _entry(PRIMARY, 222)]}}
    assert got[1] == {"prompt_tokens": 777, "completion_tokens": 10, "total_tokens": 787,
                      "context_usage": {"version": 1, "complete": True,
                                        "requests": [_entry(PRIMARY, 333), _entry(PRIMARY, 444)]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reported", [
    "absent",
    None,
    {"version": 2, "complete": True, "requests": []},
    {"version": 1, "complete": "yes", "requests": []},
    {"version": 1, "complete": True, "requests": [{"provider": "zai", "model": "glm-5.1", "input_tokens": True,
                                                    "input_limit": None}]},
    {"version": 1, "complete": True, "requests": [_entry(PRIMARY, 1)] * 129},
    # Identity ids follow the served_runtime bound: <=80 chars, no CR/LF/NUL.
    {"version": 1, "complete": True, "requests": [_entry(("zai", "m" * 81), 1)]},
    {"version": 1, "complete": True, "requests": [_entry(("p" * 81, "glm-5.1"), 1)]},
    {"version": 1, "complete": True, "requests": [_entry(("zai", "glm\r5"), 1)]},
    {"version": 1, "complete": True, "requests": [_entry(("zai", "glm\n5"), 1)]},
    {"version": 1, "complete": True, "requests": [_entry(("za\x00i", "glm-5.1"), 1)]},
])
async def test_direct_unavailable_context_usage_is_omitted_and_billing_unchanged(stream, reported):
    """An agent that reports no (or a malformed) measurement leaves ``context_usage`` absent; the
    gateway never reconstructs one, and the billing block is byte-for-byte the old one."""
    agent = MagicMock()
    agent.session_prompt_tokens, agent.session_completion_tokens, agent.session_total_tokens = 40, 2, 42
    result = {"final_response": "ok", "completed": True, "messages": []}
    if reported != "absent":
        result["context_usage"] = reported
    agent.run_conversation.return_value = result
    adapter, app = _direct_app()
    with patch.object(adapter, "_create_agent", return_value=agent):
        usage = await _chat(app, stream=stream)
    assert usage == {"prompt_tokens": 40, "completion_tokens": 2, "total_tokens": 42}


def test_wire_keeps_identity_at_the_80_char_bound():
    from gateway.platforms.api_server_usage import api_context_usage
    report = {"version": 1, "complete": True, "requests": [_entry(("p" * 80, "m" * 80), 1)]}
    assert api_context_usage(report) == report


# ── canonical session-authority path (run_api_turn) ────────────────────────────────────────────


def _canonical_handler(owner, agent):
    """The admission's handler running the REAL ``TurnRunner._run_conversation_with_approval`` (the
    executor-thread step that records the committed API usage) around ``agent``."""
    from gateway.run_turn_runner import TurnRunner

    async def handle(event):
        from gateway.session_api_turn import api_execution
        from gateway.session_results import execution_result
        assert api_execution.get() is not None  # an API admission, executed under the authority
        turn = object.__new__(TurnRunner)
        turn._ctx = SimpleNamespace(
            session_key="", session_id=event.source.chat_id, message=event.text, source=event.source,
            title_user_message=None, persist_user_display_kind=None, persist_user_display_metadata=None,
            moa_config=None, inbound_message_id=None, mute_notification_reply=False, reply_expected=None)
        turn._approval_owner = None
        turn._approval_notify_sync = lambda *args, **kwargs: None
        turn._runner = SimpleNamespace(_consume_pending_native_image_paths=lambda key: [])
        result = await asyncio.to_thread(turn._run_conversation_with_approval, agent, [], None, None, None)
        execution_result.get()["result"] = {"final_response": result["final_response"], "messages": []}
        return result["final_response"]
    return handle


def _canonical_app(api, owner, agent):
    owner.runner._handle_message = _canonical_handler(owner, agent)
    owner.adopt_agent = lambda *args: None
    app = web.Application()
    app.router.add_post("/v1/chat/completions", api._handle_chat_completions)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_canonical_two_measurements_keep_the_aggregate_sum(api, owner, stream):
    agent = _agent()
    app = _canonical_app(api, owner, agent)
    with _armed(agent, _two_call_script()):
        usage = await _chat(app, stream=stream)
    assert usage["context_usage"] == TWO_CALLS
    assert _billing(usage) == TWO_CALL_SUMS
    assert usage["prompt_tokens"] == agent.session_prompt_tokens


@pytest.mark.asyncio
async def test_canonical_stream_and_nonstream_match_the_direct_path(api, owner):
    seen = []
    for stream in (False, True):
        agent = _agent()
        app = _canonical_app(api, owner, agent)
        with _armed(agent, _two_call_script()):
            seen.append(await _chat(app, stream=stream, text=f"canonical stream={stream}"))
    direct = await _direct(_agent(), _two_call_script(), stream=False)
    assert seen[0] == seen[1] == direct == {**TWO_CALL_SUMS, "context_usage": TWO_CALLS}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_canonical_response_without_usage_is_reported_incomplete(api, owner, stream):
    agent = _agent()
    app = _canonical_app(api, owner, agent)
    with _armed(agent, _scripted(_resp("no usage"))):
        usage = await _chat(app, stream=stream)
    assert usage == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                     "context_usage": {"version": 1, "complete": False, "requests": []}}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_canonical_agent_without_context_usage_omits_it(api, owner, stream):
    agent = SimpleNamespace(session_prompt_tokens=0, session_completion_tokens=0)

    def run_conversation(message, **kwargs):
        agent.session_prompt_tokens, agent.session_completion_tokens = 40, 2
        return {"final_response": "ok", "completed": True, "messages": []}
    agent.run_conversation = run_conversation
    app = _canonical_app(api, owner, agent)
    usage = await _chat(app, stream=stream)
    assert usage == {"prompt_tokens": 40, "completion_tokens": 2, "total_tokens": 42}
