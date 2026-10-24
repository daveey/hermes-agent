"""Managed policy rejects stale paid overrides before any HTTP send."""
import asyncio
import json
from unittest.mock import patch

import anthropic
import httpx
import pytest

from agent.model_routing import ModelRoutingError, resolve_model_route
from hermes_cli.config import atomic_config_write
from hermes_cli.runtime_provider import resolve_runtime_provider
from run_agent import AIAgent
from tests.agent import test_model_routing_agent as helpers
from tests.agent.test_class_failure_wire import completion

routing_home = helpers.routing_home
streaming_route = helpers.streaming_route


@pytest.fixture
def managed(routing_home, streaming_route, tmp_path):
    routing_home["model_routing"]["enforce_subscription_only"] = True
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    return routing_home


@pytest.mark.parametrize("provider,model", [("openrouter", "vendor/paid"), ("auto", "unknown"),
                                           ("openai", "anthropic-deep")])
def test_concrete_pin_rejected_at_selector_and_runtime(managed, provider, model):
    with pytest.raises(ModelRoutingError):
        resolve_model_route(model, provider)
    with pytest.raises(ModelRoutingError):
        resolve_runtime_provider(requested=provider, target_model=model)


@pytest.mark.parametrize("surface", ["cli", "desktop", "telegram", "api", "cron", "kanban", "delegated"])
def test_stale_surface_override_cannot_construct_paid_agent(managed, surface):
    sent = []
    with patch("httpx.Client.send", side_effect=lambda request, **kw: sent.append(request)):
        with pytest.raises(ModelRoutingError):
            AIAgent(model="vendor/paid", provider="openrouter", api_key="synthetic-paid",
                    base_url="https://openrouter.ai/api/v1", platform=surface,
                    skip_context_files=True, skip_memory=True, quiet_mode=True)
    assert sent == []


@pytest.mark.parametrize("reject", [False, True])
def test_approved_exact_native_pin_uses_oauth_and_never_paid_fallback(managed, reject):
    sent = []
    original = anthropic.Anthropic

    def respond(request):
        sent.append(request)
        if reject:
            return httpx.Response(401, json={"type": "error", "error": {
                "type": "authentication_error", "message": "invalid credential"}})
        return completion(json.loads(request.content)["model"])

    def sdk(**kwargs):
        kwargs.update(http_client=httpx.Client(transport=httpx.MockTransport(respond)), max_retries=0)
        return original(**kwargs)

    with patch("anthropic.Anthropic", side_effect=sdk), \
         patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.title_generator._auto_title_enabled", return_value=False), \
         patch("agent.turn_recovery.interruptible_backoff_sleep", return_value=None):
        agent = AIAgent(model="anthropic-deep", provider="anthropic", session_id="exact-pin",
                        quiet_mode=True, skip_memory=True, skip_context_files=True, max_iterations=2,
                        fallback_model=[{"provider": "openrouter", "model": "paid"}])
        try:
            result = agent.run_conversation("Say ok")
            assert agent.model == "anthropic-deep"
            assert agent.model_routing["exact_pin"]
            assert agent._fallback_chain == []
            if not reject:
                assert result["final_response"] == "ok", result
            else:
                assert result.get("failed") or result.get("error"), result
        finally:
            agent.release_clients()
    assert sent
    assert all(r.headers.get("authorization", "").startswith("Bearer sk-ant-oat") for r in sent)
    assert all("x-api-key" not in r.headers for r in sent)


@pytest.mark.parametrize("kind", ["sync", "async", "stream"])
def test_cached_paid_auxiliary_client_is_fenced_at_relay(managed, kind):
    from agent import auxiliary_client as aux
    sent = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: sent.append(request)))
    try:
        with pytest.raises(ModelRoutingError):
            if kind == "async":
                asyncio.run(aux._relay_async_completion(client, {"model": "paid"}, provider="openrouter"))
            elif kind == "stream":
                aux._relay_sync_stream(client, {"model": "paid"}, provider="openrouter")
            else:
                aux._relay_sync_completion(client, {"model": "paid"}, provider="openrouter")
    finally:
        client.close()
    assert sent == []


def test_managed_switch_rejects_paid_pin_before_next_wire(managed):
    from agent.model_routing_request import validate_class_request
    from types import SimpleNamespace
    stale = SimpleNamespace(model="vendor/paid", provider="openrouter", model_routing=None)
    with pytest.raises(ModelRoutingError):
        validate_class_request(stale, {"model": stale.model}, streaming=False)
