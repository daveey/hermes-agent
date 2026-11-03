"""Per-provider-response context accounting (``result["context_usage"]``).

Contract: ``{version: 1, complete: bool, requests: [{provider, model, input_tokens, input_limit}]}``,
one entry per main-loop provider response measured at ``turn_usage`` (before the MoA fold), at most
128 entries, reset at every ``run_conversation`` turn. ``complete`` is False whenever a response the
turn received could not be measured individually (no usage, truncation diversion, adapter-internal
aggregation, overflow of the 128 cap). ``input_limit`` is the grounded runtime input capacity (never
the compression trigger) or ``None`` when the capacity is unknown. Billing counters are untouched.
"""

from __future__ import annotations

import json
import threading
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent

PRIMARY = ("zai", "glm-5.1")
FALLBACK = ("zai", "glm-4.7")
WINDOW = 200_000


def _tool_defs():
    return [{"type": "function", "function": {
        "name": "web_search", "description": "search", "parameters": {"type": "object", "properties": {}}}}]


def _agent(fallback_chain=None):
    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key-abcdef12", base_url="https://open.bigmodel.cn/api/coding/paas/v4",
            provider=PRIMARY[0], model=PRIMARY[1], quiet_mode=True, skip_context_files=True,
            skip_memory=True, fallback_model=fallback_chain,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent.compression_enabled = False
    agent.save_trajectories = False
    # A grounded window (as if resolved from the provider catalog); no network at turn time.
    agent.context_compressor.context_length = WINDOW
    return agent


def _usage(prompt, **extra):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=5, total_tokens=prompt + 5, **extra)


def _resp(content="done", *, prompt=None, finish="stop", tool_calls=None, usage=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    if usage is None and prompt is not None:
        usage = _usage(prompt)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)], model="served/model",
                           usage=usage)


def _tool_call(call_id="c1"):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name="web_search", arguments="{}"))


def _fake_execute(assistant_message, messages, effective_task_id, api_call_count=0):
    from agent.tool_dispatch_helpers import make_tool_result_message
    for tc in assistant_message.tool_calls:
        messages.append(make_tool_result_message("web_search", "result", tc.id))


def _run(agent, api_call, message="hello", extra=()):
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
        return agent.run_conversation(message)


def _scripted(*responses):
    queue = list(responses)

    def call(api_kwargs):
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item(api_kwargs) if callable(item) else item
    return call


def _entry(identity, tokens, limit=WINDOW):
    return {"provider": identity[0], "model": identity[1], "input_tokens": tokens, "input_limit": limit}


def _record(agent, response, api_call_count=1):
    from agent.turn_usage import record_response_usage
    return record_response_usage(agent, response, messages=[{"role": "user", "content": "hi"}],
                                 api_call_count=api_call_count, api_duration=0.1, compression_attempts=0,
                                 max_compression_attempts=3)


# ── end-to-end through run_conversation ─────────────────────────────────────────────────────────


def test_multi_call_turn_reports_each_response_with_grounded_window():
    agent = _agent()
    result = _run(agent, _scripted(
        _resp("", prompt=1_000, finish="tool_calls", tool_calls=[_tool_call()]),
        _resp("done", prompt=1_200),
    ))
    usage = result["context_usage"]
    assert usage == {"version": 1, "complete": True,
                     "requests": [_entry(PRIMARY, 1_000), _entry(PRIMARY, 1_200)]}
    # The capacity is the runtime input window, not the compression trigger.
    assert agent.context_compressor.threshold_tokens != WINDOW
    # Serializable as-is for worker transports.
    assert json.loads(json.dumps(usage)) == usage


def test_usage_resets_every_turn_and_snapshot_is_not_shared():
    agent = _agent()
    first = _run(agent, _scripted(_resp("one", prompt=700)))["context_usage"]
    second = _run(agent, _scripted(_resp("two", prompt=50)))["context_usage"]
    assert first["requests"] == [_entry(PRIMARY, 700)]
    assert second == {"version": 1, "complete": True, "requests": [_entry(PRIMARY, 50)]}


def test_response_without_usage_marks_turn_incomplete():
    agent = _agent()
    usage = _run(agent, _scripted(_resp("no usage")))["context_usage"]
    assert usage == {"version": 1, "complete": False, "requests": []}


def test_failed_attempt_then_retry_counts_only_the_received_response():
    class ReadTimeout(Exception):
        pass

    agent = _agent()
    result = _run(agent, _scripted(ReadTimeout("read timed out"), _resp("ok", prompt=300)))
    assert result["final_response"] == "ok"
    assert result["context_usage"] == {"version": 1, "complete": True, "requests": [_entry(PRIMARY, 300)]}


def test_fallback_switch_reports_the_runtime_that_answered():
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
    result = _run(agent, _scripted(RateLimitError(), _resp("via fallback", prompt=400)), extra=(
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(fb_client, FALLBACK[1])),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda m, p: m),
        patch("agent.model_metadata.get_model_context_length", return_value=WINDOW),
    ))
    assert result["final_response"] == "via fallback"
    assert agent.model == FALLBACK[1]
    assert result["context_usage"]["requests"] == [_entry(FALLBACK, 400)]
    assert result["context_usage"]["complete"] is True


def test_mid_turn_identity_change_is_per_response_and_unknown_window_is_null():
    agent = _agent()

    def switch_then_answer(api_kwargs):
        # A runtime swap the compressor has not been told about: the identity follows the
        # response, the window for the new model is unknown.
        agent.model = "glm-4.7"
        return _resp("done", prompt=900)

    result = _run(agent, _scripted(
        _resp("", prompt=800, finish="tool_calls", tool_calls=[_tool_call()]), switch_then_answer))
    assert result["context_usage"]["requests"] == [_entry(PRIMARY, 800), _entry(FALLBACK, 900, None)]


def test_truncated_response_diverted_before_measurement_marks_incomplete():
    agent = _agent()
    result = _run(agent, _scripted(
        _resp("partial answer that was cut", prompt=500, finish="length"),
        _resp(" and the rest", prompt=520),
        _resp("done", prompt=540),
    ))
    usage = result["context_usage"]
    assert usage["complete"] is False
    assert all(r["input_tokens"] in (500, 520, 540) for r in usage["requests"])


def test_invalid_response_then_valid_marks_incomplete():
    agent = _agent()
    empty = SimpleNamespace(choices=[], model="served/model", usage=_usage(10))
    usage = _run(agent, _scripted(empty, _resp("ok", prompt=310)))["context_usage"]
    assert usage["complete"] is False
    assert usage["requests"] == [_entry(PRIMARY, 310)]


def test_server_side_tools_mark_incomplete():
    agent = _agent()
    real_build = agent._build_api_kwargs

    def with_hosted_tool(*args, **kwargs):
        kwargs_out = real_build(*args, **kwargs)
        kwargs_out["tools"] = [*(kwargs_out.get("tools") or []), {"type": "web_search"}]
        return kwargs_out

    with patch.object(agent, "_build_api_kwargs", side_effect=with_hosted_tool):
        usage = _run(agent, _scripted(_resp("ok", prompt=320)))["context_usage"]
    # The hosted-tool response's usage may sum internal passes: no per-request entry.
    assert usage == {"version": 1, "complete": False, "requests": []}
    # Billing still folds it.
    assert agent.session_prompt_tokens == 320


def test_hosted_tool_response_then_ordinary_call_lists_only_the_ordinary_one():
    """Per response, not sticky: an aggregate hosted-tool response is omitted, the later ordinary
    request in the same turn is still recorded, and billing totals sum both unchanged."""
    agent = _agent()
    real_build = agent._build_api_kwargs
    calls = {"n": 0}

    def hosted_on_first_call(*args, **kwargs):
        kwargs_out = real_build(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            kwargs_out["tools"] = [*(kwargs_out.get("tools") or []), {"type": "web_search"}]
        return kwargs_out

    script = _scripted(_resp(None, prompt=900, finish="tool_calls", tool_calls=[_tool_call()]),
                       _resp("ok", prompt=400))
    with patch.object(agent, "_build_api_kwargs", side_effect=hosted_on_first_call):
        usage = _run(agent, script)["context_usage"]
    assert calls["n"] == 2
    assert usage == {"version": 1, "complete": False, "requests": [_entry(PRIMARY, 400)]}
    assert agent.session_prompt_tokens == 1_300
    assert agent.session_completion_tokens == 10
    assert agent.session_api_calls == 2


def test_unmeasurable_flag_skips_only_the_context_entry():
    from agent import context_usage
    from agent.turn_usage import record_response_usage
    agent = _agent()
    context_usage.begin_turn_context_usage(agent)
    record_response_usage(agent, _resp(prompt=50), messages=[{"role": "user", "content": "hi"}],
                          api_call_count=1, api_duration=0.1, compression_attempts=0,
                          max_compression_attempts=3, context_measurable=False)
    _record(agent, _resp(prompt=60), api_call_count=2)
    assert context_usage.export_turn_context_usage(agent)["requests"] == [_entry(PRIMARY, 60)]
    assert agent.session_prompt_tokens == 110


@pytest.mark.parametrize("bad", ["m" * 81, "glm\r5", "glm\n5", "glm\x005"])
@pytest.mark.parametrize("field", ["provider", "model"])
def test_wire_unsafe_identity_is_unresolvable(field, bad):
    from agent import context_usage
    agent = _agent()
    setattr(agent, field, bad)
    context_usage.begin_turn_context_usage(agent)
    _record(agent, _resp(prompt=70))
    assert context_usage.export_turn_context_usage(agent) == {"version": 1, "complete": False, "requests": []}


def test_identity_at_the_80_char_bound_is_kept():
    from agent import context_usage
    agent = _agent()
    agent.model = "m" * 80
    context_usage.begin_turn_context_usage(agent)
    _record(agent, _resp(prompt=70))
    assert context_usage.export_turn_context_usage(agent)["requests"] == [
        _entry((PRIMARY[0], "m" * 80), 70, None)]


# ── turn_usage capture seam ─────────────────────────────────────────────────────────────────────


def test_capture_is_a_noop_outside_a_turn():
    from agent import context_usage
    agent = _agent()
    _record(agent, _resp(prompt=10))
    assert context_usage.export_turn_context_usage(agent) is None


def test_moa_records_aggregator_before_advisor_fold_and_keeps_billing():
    from agent import context_usage
    from agent.usage_pricing import CanonicalUsage

    agent = _agent()
    agent.provider, agent.model = "moa", "my-preset"
    moa = MagicMock(spec=["consume_reference_usage", "last_aggregator_slot", "consume_and_save_trace"])
    moa.consume_reference_usage.return_value = (CanonicalUsage(input_tokens=9_000), None)
    moa.last_aggregator_slot = {"provider": "anthropic", "model": "claude-opus-4-1"}
    agent.client = moa
    context_usage.begin_turn_context_usage(agent)
    _record(agent, _resp(prompt=700))
    usage = context_usage.export_turn_context_usage(agent)
    # Aggregator identity and prompt; advisor calls are aggregated, so not individually measurable.
    assert usage == {"version": 1, "complete": False,
                     "requests": [_entry(("anthropic", "claude-opus-4-1"), 700, None)]}
    # Billing still folds advisor spend.
    assert agent.session_prompt_tokens == 9_700


def test_moa_without_advisor_calls_stays_complete():
    from agent import context_usage
    from agent.usage_pricing import CanonicalUsage

    agent = _agent()
    agent.provider, agent.model = "moa", "my-preset"
    moa = MagicMock(spec=["consume_reference_usage", "last_aggregator_slot", "consume_and_save_trace"])
    moa.consume_reference_usage.return_value = (CanonicalUsage(), None)
    moa.last_aggregator_slot = {"provider": "anthropic", "model": "claude-opus-4-1"}
    agent.client = moa
    context_usage.begin_turn_context_usage(agent)
    _record(agent, _resp(prompt=700))
    assert context_usage.export_turn_context_usage(agent)["complete"] is True


def test_moa_without_aggregator_slot_never_reports_the_virtual_identity():
    from agent import context_usage
    agent = _agent()
    agent.provider, agent.model = "moa", "my-preset"
    agent.client = MagicMock(spec=["chat"])
    context_usage.begin_turn_context_usage(agent)
    _record(agent, _resp(prompt=700))
    assert context_usage.export_turn_context_usage(agent) == {"version": 1, "complete": False, "requests": []}


def test_class_selector_identity_is_never_reported():
    from agent import context_usage
    agent = _agent()
    agent.model = "class:deep"
    context_usage.begin_turn_context_usage(agent)
    _record(agent, _resp(prompt=100))
    assert context_usage.export_turn_context_usage(agent) == {"version": 1, "complete": False, "requests": []}


def test_requests_are_capped_at_128_and_overflow_is_incomplete():
    from agent import context_usage
    agent = _agent()
    context_usage.begin_turn_context_usage(agent)
    for i in range(130):
        _record(agent, _resp(prompt=100 + i), api_call_count=i + 1)
    usage = context_usage.export_turn_context_usage(agent)
    assert len(usage["requests"]) == 128
    assert usage["requests"][-1]["input_tokens"] == 227
    assert usage["complete"] is False


def test_zero_usage_sentinel_is_not_a_measurement():
    """ACP / external-process shims return all-zero usage for a whole remote agent run."""
    from agent import context_usage
    agent = _agent()
    context_usage.begin_turn_context_usage(agent)
    _record(agent, _resp(prompt=0))
    assert context_usage.export_turn_context_usage(agent) == {"version": 1, "complete": False, "requests": []}


def test_gemini_multi_hop_records_first_hop_and_marks_incomplete():
    from agent import context_usage
    agent = _agent()
    context_usage.begin_turn_context_usage(agent)
    _record(agent, _resp(usage=_usage(32_968, initial_prompt_tokens=100, initial_cached_tokens=0)))
    assert context_usage.export_turn_context_usage(agent) == {
        "version": 1, "complete": False, "requests": [_entry(PRIMARY, 100)]}


def test_gemini_single_hop_stays_complete():
    from agent import context_usage
    agent = _agent()
    context_usage.begin_turn_context_usage(agent)
    _record(agent, _resp(usage=_usage(100, initial_prompt_tokens=100, initial_cached_tokens=0)))
    assert context_usage.export_turn_context_usage(agent)["complete"] is True


def test_input_limit_subtracts_output_reservation():
    from agent import context_usage
    agent = _agent()
    agent.context_compressor.max_tokens = 32_000
    context_usage.begin_turn_context_usage(agent)
    _record(agent, _resp(prompt=10))
    assert context_usage.export_turn_context_usage(agent)["requests"][0]["input_limit"] == WINDOW - 32_000


def test_guessed_windows_are_null():
    from agent import context_usage, model_metadata

    agent = _agent()
    comp = agent.context_compressor
    # Guessed probe tier (step-down without a provider-reported limit).
    comp._context_probed, comp._context_probe_persistable = True, False
    context_usage.begin_turn_context_usage(agent)
    _record(agent, _resp(prompt=10))
    assert context_usage.export_turn_context_usage(agent)["requests"][0]["input_limit"] is None

    # Provider-reported limit is grounded.
    comp._context_probed, comp._context_probe_persistable = True, True
    context_usage.begin_turn_context_usage(agent)
    with patch("agent.model_metadata.save_provider_context_length"):
        _record(agent, _resp(prompt=10))
    assert context_usage.export_turn_context_usage(agent)["requests"][0]["input_limit"] == WINDOW

    # Detection-failed hard default.
    comp.context_length = model_metadata.DEFAULT_FALLBACK_CONTEXT
    key = (agent.model, agent.base_url)
    model_metadata._FALLBACK_WARNED.add(key)
    try:
        context_usage.begin_turn_context_usage(agent)
        _record(agent, _resp(prompt=10))
        assert context_usage.export_turn_context_usage(agent)["requests"][0]["input_limit"] is None
    finally:
        model_metadata._FALLBACK_WARNED.discard(key)


def test_codex_app_server_turn_is_adapter_aggregated():
    from agent import context_usage
    from agent.codex_runtime import _record_codex_app_server_usage

    agent = _agent()
    context_usage.begin_turn_context_usage(agent)
    _record_codex_app_server_usage(agent, SimpleNamespace(token_usage_last={"inputTokens": 5_000, "outputTokens": 9}))
    assert context_usage.export_turn_context_usage(agent) == {"version": 1, "complete": False, "requests": []}


def test_iteration_summary_response_is_captured():
    from agent import context_usage
    from agent.chat_completion_helpers import _summary_text

    agent = _agent()
    context_usage.begin_turn_context_usage(agent)
    assert _summary_text(agent, _resp("summary", prompt=4_000)) == "summary"
    assert context_usage.export_turn_context_usage(agent) == {
        "version": 1, "complete": True, "requests": [_entry(PRIMARY, 4_000)]}


def test_concurrent_agents_and_forks_are_isolated():
    from agent import context_usage

    a, b, fork = _agent(), _agent(), _agent()
    context_usage.begin_turn_context_usage(a)
    context_usage.begin_turn_context_usage(b)
    barrier = threading.Barrier(3)

    def work(agent, base):
        barrier.wait()
        for i in range(40):
            _record(agent, _resp(prompt=base + i), api_call_count=i + 1)

    threads = [threading.Thread(target=work, args=(ag, base)) for ag, base in ((a, 1_000), (b, 5_000), (fork, 9_000))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ua, ub = context_usage.export_turn_context_usage(a), context_usage.export_turn_context_usage(b)
    assert [r["input_tokens"] for r in ua["requests"]] == list(range(1_000, 1_040))
    assert [r["input_tokens"] for r in ub["requests"]] == list(range(5_000, 5_040))
    assert ua["complete"] and ub["complete"]
    # A fork that never began a turn records nothing anywhere.
    assert context_usage.export_turn_context_usage(fork) is None


@pytest.mark.parametrize("bad", [None, MagicMock()])
def test_export_tolerates_foreign_state(bad):
    from agent import context_usage
    agent = SimpleNamespace(_turn_context_usage=bad)
    assert context_usage.export_turn_context_usage(agent) is None
