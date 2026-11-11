"""A route context attestation is a compressible limit, not an authorization failure."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.model_routing import ModelRoutingError
from agent.model_routing_request import validate_class_request
from tests.agent import test_413_compression as helpers


@pytest.fixture
def route(monkeypatch):
    entry = {"provider": "openai-codex", "model": "gpt-6-astra", "billing": "subscription",
             "eligible": True, "funding_verified": True, "weight": 1,
             "capabilities": {"tools": True, "streaming": True, "context_tokens": 272000}}
    policy = {"enabled": True, "policy_version": 1, "routes": {"deep": [entry]}}
    monkeypatch.setattr("agent.model_routing_managed.verify_managed_agent", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective",
                        lambda: {"model_routing": policy})
    agent = SimpleNamespace(model="gpt-6-astra", provider="openai-codex", max_tokens=8000,
                            _model_routing_requirements={},
                            model_routing={"selected_model": "gpt-6-astra",
                                           "selected_provider": "openai-codex",
                                           "requested_class": "deep", "billing_group": "subscription"})
    return agent, policy, entry


def refusal(agent, **request):
    with pytest.raises(ModelRoutingError) as raised:
        validate_class_request(agent, {"model": agent.model, "messages": [
            {"role": "user", "content": "x" * 1072000}], "max_tokens": 8000, **request}, streaming=True)
    return raised.value


def test_context_only_refusal_is_compressible(route):
    agent, _, _ = route
    error = refusal(agent)
    verdict = classify_api_error(error, provider=agent.provider, model=agent.model)
    assert verdict.reason == FailoverReason.context_overflow
    assert verdict.should_compress
    assert not verdict.should_rotate_credential
    assert not verdict.should_fallback


@pytest.mark.parametrize("target,key,value,extra", [
    ("caps", "tools", False, {"tools": [{"type": "function", "function": {"name": "t"}}]}),
    ("policy", "enabled", False, {}),
    ("entry", "funding_verified", False, {}),
    ("entry", "eligible", False, {}),
    ("metadata", "billing_group", "metered", {}),
    ("requirements", "min_context_tokens", 300000, {}),
])
def test_non_context_policy_refusals_remain_terminal(route, target, key, value, extra):
    agent, policy, entry = route
    objects = {"caps": entry["capabilities"], "policy": policy, "entry": entry,
               "metadata": agent.model_routing, "requirements": agent._model_routing_requirements}
    objects[target][key] = value
    assert not classify_api_error(refusal(agent, **extra)).should_compress


def test_metered_refusal_does_not_compress(route):
    agent, _, entry = route
    agent.model_routing["billing_group"] = entry["billing"] = "metered"
    assert not classify_api_error(refusal(agent)).should_compress


def test_error_text_alone_does_not_authorize_compression():
    error = ModelRoutingError("failing capability: min_context_tokens(required=276008, attested=272000)")
    assert not classify_api_error(error).should_compress


@pytest.mark.parametrize("exhausted", [False, True])
def test_route_refusal_compacts_once_then_succeeds(route, monkeypatch, exhausted):
    error = refusal(route[0])
    agent = helpers._new_test_agent()
    agent.max_compression_attempts = 1
    agent.context_compressor.threshold_tokens = 1000000
    agent.client.chat.completions.create.side_effect = [
        error, error if exhausted else helpers._mock_response("Recovered")]
    history = [{"role": "user", "content": "x" * 1072000},
               {"role": "assistant", "content": "working"}]
    monkeypatch.setattr("agent.turn_overflow.time.sleep", lambda *_: None)
    with patch.object(agent, "_compress_context", return_value=([
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "summary"}], "You are helpful.")) as compress, \
         patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"), \
         patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("continue", conversation_history=history)
    if exhausted:
        assert result.get("failed")
        assert result.get("compression_exhausted")
    else:
        assert not result.get("failed"), result
        assert result["final_response"] == "Recovered"
    assert compress.call_count == 1
    assert agent.client.chat.completions.create.call_count == 2
