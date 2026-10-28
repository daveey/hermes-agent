"""Turning managed enforcement on must not strand live agents, and aux allocations must not lock
main sessions to one provider. Both were found enabling the fleet policy on live gateways."""
import json
from unittest.mock import patch

import anthropic
import httpx
import pytest

from agent.model_routing_state import RoutingState
from hermes_cli.config import atomic_config_write
from run_agent import AIAgent
from tests.agent import test_managed_subscription as fixtures
from tests.agent.test_class_failure_wire import completion

routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route

OAUTH = "sk-ant-oat01-synthetic-oauth"
PAID = "sk-ant-api03-synthetic-paid"


def _route(provider, model):
    return {"provider": provider, "model": model, "billing": "subscription", "weight": 1,
            "eligible": True, "funding_verified": True, "capabilities": {}}


def test_aux_allocations_do_not_lock_main_sessions_to_one_provider(tmp_path):
    state = RoutingState(tmp_path / "model-routing.sqlite3")
    candidates = [_route("anthropic", "deep-a"), _route("openai-codex", "deep-b")]
    mains = []
    for i in range(6):  # every session: one main allocation, then its one aux allocation
        mains.append(state.allocate(f"s{i}", None, "deep", candidates, 1, set())["selected_provider"])
        state.allocate(f"aux:s{i}:coach_defect:class:deep", None, "deep", candidates, 1, set())
    assert mains.count("anthropic") == mains.count("openai-codex") == 3, mains


def test_main_counter_position_survives_the_upgrade(tmp_path):
    state = RoutingState(tmp_path / "model-routing.sqlite3")
    candidates = [_route("anthropic", "deep-a"), _route("openai-codex", "deep-b")]
    first = state.allocate("s0", None, "deep", candidates, 1, set())["selected_provider"]
    second = state.allocate("s1", None, "deep", candidates, 1, set())["selected_provider"]
    assert {first, second} == {"anthropic", "openai-codex"}


@pytest.fixture
def unenforced(routing_home, streaming_route, tmp_path):
    routing_home["model_routing"]["enforce_subscription_only"] = False
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    return routing_home


def _enforce(cfg, tmp_path):
    cfg["model_routing"]["enforce_subscription_only"] = True
    atomic_config_write(tmp_path / "config.yaml", cfg)


def _sdk_recording(sent):
    original = anthropic.Anthropic

    def respond(request):
        sent.append(request)
        body = json.loads(request.content)
        if body.get("stream"):
            return completion(body["model"])
        return httpx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1}})

    def sdk(**kwargs):
        kwargs.update(http_client=httpx.Client(transport=httpx.MockTransport(respond)), max_retries=0)
        return original(**kwargs)
    return sdk


def _patches(sdk):
    return (patch("anthropic.Anthropic", side_effect=sdk),
            patch("model_tools.get_tool_definitions", return_value=[]),
            patch("agent.title_generator._auto_title_enabled", return_value=False),
            patch("agent.turn_recovery.interruptible_backoff_sleep", return_value=None))


@pytest.mark.parametrize("stream", [False, True])
def test_live_agent_adopts_policy_when_enforcement_is_switched_on(unenforced, tmp_path, stream):
    sent = []
    p1, p2, p3, p4 = _patches(_sdk_recording(sent))
    with p1, p2, p3, p4:
        agent = AIAgent(model="anthropic-deep", provider="anthropic", session_id="live-before-flip",
                        quiet_mode=True, skip_memory=True, skip_context_files=True, max_iterations=2)
        agent._disable_streaming = not stream
        try:
            assert agent.model_routing is None  # built while enforcement was off: no provenance
            _enforce(unenforced, tmp_path)      # operator flips the fleet policy under a live agent
            result = agent.run_conversation("Say ok")
            assert result["final_response"] == "ok", result
            assert agent.model_routing["exact_pin"] is True
            assert agent.model_routing["billing_group"] == "subscription"
            assert agent._fallback_chain == []
            assert sent
            for request in sent:
                assert request.headers.get("authorization") == "Bearer " + OAUTH
                assert PAID not in json.dumps(dict(request.headers))
        finally:
            agent.release_clients()


def test_live_agent_with_unapproved_pin_fails_closed_after_flip(unenforced, tmp_path):
    sent = []
    with patch("httpx.Client.send", side_effect=lambda request, **kw: sent.append(request)), \
         patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.title_generator._auto_title_enabled", return_value=False), \
         patch("agent.turn_recovery.interruptible_backoff_sleep", return_value=None):
        agent = AIAgent(model="vendor/paid", provider="openrouter", api_key="synthetic-paid",
                        base_url="https://openrouter.ai/api/v1", session_id="paid-before-flip",
                        quiet_mode=True, skip_memory=True, skip_context_files=True, max_iterations=2)
        try:
            _enforce(unenforced, tmp_path)
            result = agent.run_conversation("Say ok")
            assert result.get("failed") or result.get("error"), result
            assert agent.model_routing is None
            # Construction may fetch the public model catalog; no completion may ever be sent.
            assert [r for r in sent if r.method != "GET"] == []
        finally:
            agent.release_clients()


def test_adoption_is_a_noop_while_enforcement_is_off(unenforced):
    from agent.model_routing_agent import adopt_managed_policy
    sent = []
    p1, p2, p3, p4 = _patches(_sdk_recording(sent))
    with p1, p2, p3, p4:
        agent = AIAgent(model="anthropic-deep", provider="anthropic", session_id="never-flipped",
                        quiet_mode=True, skip_memory=True, skip_context_files=True, max_iterations=2)
        try:
            assert adopt_managed_policy(agent) is False
            assert agent.model_routing is None
        finally:
            agent.release_clients()
