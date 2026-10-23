"""A revoked subscription can fail over only to an eligible same-class provider."""
import json
from copy import deepcopy
from unittest.mock import patch

import anthropic
import httpx

from hermes_cli.config import atomic_config_write
from run_agent import AIAgent
from tests.agent import test_model_routing_agent
from tests.agent.test_model_routing_agent import build_agent
from tests.agent.test_codex_usage_attribution import _jwt

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route


def add_codex_subscription(routing_home, tmp_path):
    codex = deepcopy(routing_home["model_routing"]["routes"]["deep"][0])
    codex.update(provider="openai-codex", model="gpt-5.4")
    routing_home["model_routing"]["routes"]["deep"].append(codex)
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    auth_path = tmp_path / "auth.json"
    auth = json.loads(auth_path.read_text())
    token = _jwt()
    auth["credential_pool"]["openai-codex"] = [{
        "id": "codex-subscription", "label": "codex-subscription", "priority": 0,
        "auth_type": "oauth", "source": "manual", "access_token": token,
        "base_url": "https://chatgpt.com/backend-api/codex",
    }]
    auth_path.write_text(json.dumps(auth))
    return token


def test_account_count_does_not_change_provider_share(routing_home, streaming_route, tmp_path):
    from collections import Counter
    from hermes_cli.runtime_provider import resolve_runtime_provider

    add_codex_subscription(routing_home, tmp_path)
    auth_path = tmp_path / "auth.json"
    auth = json.loads(auth_path.read_text())
    entries = auth["credential_pool"]["anthropic"]
    subscription = next(entry for entry in entries if entry["auth_type"] == "oauth")
    entries.extend(dict(subscription, id=f"extra-{i}", label=f"extra-{i}") for i in range(3))
    auth_path.write_text(json.dumps(auth))
    counts = Counter(resolve_runtime_provider(
        requested="auto", target_model="class:deep", routing_session_id=f"account-share-{i}"
    )["provider"] for i in range(12))
    assert counts == {"anthropic": 6, "openai-codex": 6}


def test_auth_failure_cross_provider_native_wire(routing_home, streaming_route, tmp_path):
    token = add_codex_subscription(routing_home, tmp_path)
    sent, native_clients = [], []

    def respond(request):
        sent.append(request)
        if request.url.host == "api.anthropic.com":
            return httpx.Response(401, json={"type": "error", "error": {
                "type": "authentication_error", "message": "invalid credential"}})
        assert request.url.host == "chatgpt.com"
        response = {"id": "resp_test", "object": "response", "created_at": 0,
                    "status": "completed", "model": "gpt-5.4", "output": [{
                        "type": "message", "id": "msg_test", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}]}]}
        events = [{"type": "response.output_item.done", "output_index": 0, "item": response["output"][0]},
                  {"type": "response.completed", "response": response}]
        content = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=content + "data: [DONE]\n\n")

    def http_client(*args, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(respond))

    sdk_class = anthropic.Anthropic

    def sdk(**kwargs):
        kwargs.update(http_client=http_client(), max_retries=0)
        client = sdk_class(**kwargs)
        native_clients.append(client)
        return client

    with patch("model_tools.get_tool_definitions", return_value=[]), \
            patch("agent.title_generator._auto_title_enabled", return_value=False), \
            patch("agent.turn_recovery.interruptible_backoff_sleep", return_value=None), \
            patch("socket.socket.connect", side_effect=AssertionError("live network prohibited")), \
            patch.object(AIAgent, "_build_keepalive_http_client", staticmethod(http_client)), \
            patch("anthropic.Anthropic", side_effect=sdk):
        agent = build_agent(session_id="cross-provider", max_iterations=4)
        try:
            assert agent.provider == "anthropic"
            result = agent.run_conversation("Say ok")
            assert result["final_response"] == "ok", result
            assert agent.provider == "openai-codex"
            assert agent.model_routing["requested_class"] == "deep"
            assert agent.model_routing["billing_group"] == "subscription"
            assert agent.model_routing["reason"] == "same-class-failover"
            resumed = build_agent(session_id="cross-provider")
            try:
                assert resumed.model == agent.model == "gpt-5.4"
                assert resumed.provider == agent.provider
            finally:
                resumed.release_clients()
        finally:
            agent.release_clients()
    assert {request.url.host for request in sent} == {"api.anthropic.com", "chatgpt.com"}
    for request in sent:
        assert "x-api-key" not in request.headers
        body = json.loads(request.content)
        assert not body["model"].startswith("class:")
        if request.url.host == "chatgpt.com":
            assert request.url.path == "/backend-api/codex/responses"
            assert request.headers["authorization"] == "Bearer " + token
            assert request.headers["chatgpt-account-id"] == "acct-attribution-test"
        else:
            assert request.headers["authorization"].startswith("Bearer sk-ant-oat")
    assert all(client.is_closed() for client in native_clients)
