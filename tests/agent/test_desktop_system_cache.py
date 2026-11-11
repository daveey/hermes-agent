"""Full-system Desktop layout keeps tools and rolling transaction reuse."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.prompt_caching import build_prompt_cache_plan, _count_cache_markers
from tests.agent.test_prompt_caching import _tool_heavy_native_tools, MARKER
from tests.agent import test_class_failure_wire as wire

routing_home = wire.routing_home
streaming_route = wire.streaming_route


def _plan(messages):
    return build_prompt_cache_plan(
        messages, _tool_heavy_native_tools(), native_anthropic=True,
        direct_native_tool_cache=True, static_system_prefix="stable prefix",
        cache_full_system=True,
    )


def test_fresh_sessions_share_marked_system_and_rounds_keep_wire_bytes():
    history = [
        {"role": "system", "content": "stable prefix\nshared context and skills"},
        {"role": "user", "content": "first distinct request"},
    ]
    first = _plan(history)
    system = first.messages[0]
    assert "cache_control" not in system["content"][0]
    assert system["content"][-1]["cache_control"] == MARKER
    second_history = copy.deepcopy(history)
    second_history[-1]["content"] = "second distinct request"
    # Explicit marker at the same tools+system prefix lets another fresh session read it;
    # this is a plan-level contract, not fabricated provider cache accounting.
    second = _plan(second_history)
    assert second.tools == first.tools
    assert second.messages[0] == system
    expected_wire = json.dumps(system, sort_keys=True)
    previous_endpoint = None
    for round_no in range(4):
        ids = [f"r{round_no}-{i}" for i in range(25)]
        history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": key, "type": "function", "function": {"name": "tool_00", "arguments": "{}"}}
            for key in ids
        ]})
        history.extend({"role": "tool", "tool_call_id": key, "content": "result"} for key in ids)
        plan = _plan(history)
        assert json.dumps(plan.messages[0], sort_keys=True) == expected_wire
        assert plan.tools[-1]["cache_control"] == MARKER
        assert _count_cache_markers(plan.messages, plan.tools) <= 4
        assert plan.messages[-1]["cache_control"] == MARKER
        if previous_endpoint is not None:
            assert plan.messages[previous_endpoint]["cache_control"] == MARKER
        previous_endpoint = len(history) - 1
        assert _plan(plan.messages) == plan


@pytest.mark.parametrize("platform,source,attended,expected", [
    ("desktop", "desktop", True, True), ("local", "desktop", True, True),
    ("subagent", "desktop", False, False), ("local", "kanban", False, False),
    ("cli", "cli", True, False), ("desktop", "desktop", False, False),
])
def test_layout_is_pinned_at_init_for_attended_desktop_only(platform, source, attended, expected):
    from agent.agent_init import _init_prompt_cache_config
    agent = SimpleNamespace(platform=platform, _anthropic_prompt_cache_policy=lambda: (True, True))
    with patch("agent.session_source.session_source_for", return_value=source), \
         patch("agent.session_source.is_attended", return_value=attended), \
         patch("hermes_cli.config.load_config_readonly", return_value={}):
        _init_prompt_cache_config(agent)
    assert agent._cache_full_system is expected


def test_failover_redecoration_keeps_pinned_full_system_layout():
    from agent.conversation_loop import _redecorate_prompt_cache_for_provider
    history = [{"role": "system", "content": "stable prefix\nshared context and skills"},
               {"role": "user", "content": "hello"}]
    original = _plan(history)
    agent = SimpleNamespace(
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-4-5",
        _use_prompt_caching=True, _use_native_cache_layout=True, _cache_ttl="5m",
        _cache_full_system=True, _cached_system_prompt_static="stable prefix",
        _cached_system_prompt=history[0]["content"], tools=_tool_heavy_native_tools(),
        _direct_native_anthropic_tool_cache_capability=lambda: True,
    )
    messages, _, tools = _redecorate_prompt_cache_for_provider(agent, original.messages)
    assert messages[0] == original.messages[0]
    assert tools == original.tools


def test_desktop_wire_marks_full_system_without_changing_rounds(routing_home, streaming_route):
    import anthropic
    import httpx

    sent = []
    assembled = []
    def capture_plan(*args, **kwargs):
        plan = build_prompt_cache_plan(*args, **kwargs)
        assembled.append(plan)
        return plan
    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        return wire.completion(body["model"], len(sent) if len(sent) <= 2 else None)
    sdk_class = anthropic.Anthropic
    def sdk(**kwargs):
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(respond))
        return sdk_class(**kwargs)
    schema = {"type": "function", "function": {"name": "read_file", "description": "Read fixture",
              "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
    with patch("model_tools.get_tool_definitions", return_value=[schema]), \
         patch("model_tools.handle_function_call", return_value="fixture contents"), \
         patch("agent.title_generator._auto_title_enabled", return_value=False), \
         patch("agent.session_source.session_source_for", return_value="desktop"), \
         patch("gateway.session_finite.finite_turn_required", return_value=False), \
         patch("agent.turn_request_assembly.build_prompt_cache_plan", side_effect=capture_plan), \
         patch("anthropic.Anthropic", side_effect=sdk):
        agent = wire.build_agent(session_id="desktop-cache-layout", platform="desktop", max_iterations=4)
        try:
            assert agent._cache_full_system is True
            result = agent.run_conversation("Read both fixtures then say ok")
            assert result["final_response"] == "ok", result
            assert len(sent) == 3
            assert assembled
            assert all(p.messages[0]["content"][-1].get("cache_control") for p in assembled)
            assert sent[0]["system"][-1].get("cache_control")
            assert "cache_control" not in sent[0]["system"][0]
            assert all(body["system"] == sent[0]["system"] for body in sent)
            assert sent[2]["messages"][:len(sent[1]["messages"])] == sent[1]["messages"]
        finally:
            agent.release_clients()


@pytest.mark.parametrize("parent_layout", [False, True])
def test_cache_parity_fork_inherits_pinned_layout(parent_layout, monkeypatch):
    from agent.background_review import build_cache_parity_fork
    from tests.agent.test_background_review import _bare_agent, FakeReviewAgent
    import run_agent
    parent = _bare_agent()
    parent._cache_full_system = parent_layout
    monkeypatch.setattr(run_agent, "AIAgent", FakeReviewAgent)
    fork, _, routed = build_cache_parity_fork(parent, max_iterations=1)
    assert not routed
    assert fork._cache_full_system is parent_layout


@pytest.mark.parametrize("content,prefix", [
    ("stable prefix", "stable prefix"), ("stable prefix  \n", "stable prefix"),
    ("shared system", None), ("shared system", "different prefix"),
    ([{"type": "text", "text": "stable prefix"},
      {"type": "text", "text": "\nshared suffix"}], "stable prefix"),
])
def test_full_system_layout_has_exactly_one_system_marker(content, prefix):
    messages = [{"role": "system", "content": content}, {"role": "user", "content": "hello"}]
    original = copy.deepcopy(messages)
    plan = build_prompt_cache_plan(messages, _tool_heavy_native_tools(), native_anthropic=True,
                                  direct_native_tool_cache=True, static_system_prefix=prefix,
                                  cache_full_system=True)
    parts = plan.messages[0]["content"]
    assert all(p["text"] for p in parts)
    assert parts[-1]["cache_control"] == MARKER
    assert sum("cache_control" in p for p in parts) == 1
    assert _count_cache_markers(plan.messages, plan.tools) == 3
    assert messages == original
