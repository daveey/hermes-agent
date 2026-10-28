"""[daveey] A core-update pause ends a turn only at a step boundary.

The boundary is after every tool result of the step is appended and persisted, and before the next model
call; never during a running tool or a model call. ``agent.step_boundary_pause`` (installed per turn by the
Desktop/TUI gateway) is consulted there; a truthy reason ends the turn as an interrupt the gateway can
auto-continue.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _tool_call(i: int) -> SimpleNamespace:
    return SimpleNamespace(id=f"call_{i}", type="function",
                           function=SimpleNamespace(name="web_search", arguments='{"query": "x"}'))


def _tool_response(i: int) -> SimpleNamespace:
    message = SimpleNamespace(content=None, reasoning_content=None, reasoning=None, tool_calls=[_tool_call(i)])
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
                           model="test/model", usage=None)


def _final_response() -> SimpleNamespace:
    message = SimpleNamespace(content="done", reasoning_content=None, reasoning=None, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                           model="test/model", usage=None)


def _tool_definition() -> dict:
    return {"type": "function", "function": {
        "name": "web_search", "description": "Search the web",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}


def _make_agent(events: list, tool_seconds: float = 0.0, on_tool_start=None):
    with (
        patch("model_tools.get_tool_definitions", return_value=[_tool_definition()]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("agent.model_metadata.get_model_context_length", return_value=256_000),
        patch("agent.context_compressor.get_model_context_length", return_value=256_000),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                        model="test/model", quiet_mode=True, skip_context_files=True, skip_memory=True,
                        max_iterations=10)
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.max_compression_attempts = 1
    compressor = MagicMock()
    compressor.protect_first_n = 3
    compressor.protect_last_n = 20
    compressor.threshold_tokens = 999_999_999
    compressor.context_length = 1_000_000_000
    compressor.last_prompt_tokens = -1
    compressor._verify_compaction_cleared_threshold = False
    compressor.awaiting_real_usage_after_compression = False
    compressor.should_compress.return_value = False
    compressor.should_compress_info.return_value = (False, None)
    compressor.should_compress_preflight.return_value = False
    compressor.should_defer_preflight_to_real_usage.return_value = False
    compressor.get_active_compression_failure_cooldown.return_value = None
    compressor.select_context.return_value = None
    compressor.get_automatic_compaction_status_message.return_value = ""
    agent.compression_enabled = False
    agent.context_compressor = compressor

    def _execute(assistant_message, messages, *_args):
        call = assistant_message.tool_calls[0]
        events.append(("tool_start", call.id))
        if on_tool_start is not None:
            on_tool_start()
        time.sleep(tool_seconds)
        messages.append({"role": "tool", "name": call.function.name, "tool_call_id": call.id, "content": "ok"})
        events.append(("tool_end", call.id))

    agent._execute_tool_calls = _execute
    return agent


def _run(agent, responses, events):
    agent.client.chat.completions.create.side_effect = responses

    def _persist(messages, *_a, **_k):
        events.append(("persist", [m.get("role") for m in messages]))

    with (
        patch.object(agent, "_flush_messages_to_session_db", return_value=True),
        patch.object(agent, "_persist_session", side_effect=_persist),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation("do some tool work")


def test_pause_requested_during_a_running_tool_ends_the_turn_after_it():
    """The request lands while a 2-s tool runs: the tool finishes, its result is persisted, then the turn
    ends with no further model call."""
    events: list = []
    requested = threading.Event()

    def _hook():
        events.append(("hook", requested.is_set()))
        return "core_update_pause" if requested.is_set() else None

    agent = _make_agent(events, tool_seconds=2.0, on_tool_start=requested.set)
    agent.step_boundary_pause = _hook
    started = time.monotonic()
    result = _run(agent, [_tool_response(1), _tool_response(2), _final_response()], events)

    assert time.monotonic() - started >= 2.0  # the tool ran to completion
    assert agent.client.chat.completions.create.call_count == 1
    assert result["interrupted"] is True
    assert result["completed"] is False
    assert result["paused_for_update"] == "core_update_pause"
    names = [e[0] for e in events]
    # Consulted only at the boundary: after the tool ended, never while it ran.
    assert names.index("tool_end") < names.index("hook")
    hook_at = names.index("hook")
    # The tool result was persisted at the boundary, before the turn ended.
    assert any(e[0] == "persist" and e[1][-1] == "tool" for e in events[hook_at:])


def test_hook_declining_lets_the_turn_run_to_completion():
    events: list = []
    agent = _make_agent(events)
    agent.step_boundary_pause = lambda: None
    result = _run(agent, [_tool_response(1), _final_response()], events)
    assert agent.client.chat.completions.create.call_count == 2
    assert result["completed"] is True and result["final_response"] == "done"
    assert not result.get("paused_for_update")


def test_no_hook_is_unchanged():
    events: list = []
    agent = _make_agent(events)
    result = _run(agent, [_tool_response(1), _final_response()], events)
    assert result["completed"] is True
    assert "paused_for_update" not in result


def test_not_consulted_before_the_first_model_call():
    """A turn that has not run a step yet has no boundary to pause at (nothing to continue from)."""
    events: list = []
    calls: list = []
    agent = _make_agent(events)
    agent.step_boundary_pause = lambda: calls.append(1) or None
    _run(agent, [_final_response()], events)
    assert calls == []
