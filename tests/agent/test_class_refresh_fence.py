"""Request-time credentials and recovery episodes preserve class billing."""
import json
from copy import deepcopy
from unittest.mock import patch, MagicMock

import anthropic
import httpx
import pytest

from hermes_cli.config import atomic_config_write
from tests.agent import test_model_routing_agent
from tests.agent.test_class_failure_wire import completion

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route
build_agent = test_model_routing_agent.build_agent


@pytest.mark.parametrize("reject", [False, True, "rotate"])
def test_paid_env_never_replaces_subscription_at_dispatch(routing_home, streaming_route, monkeypatch, tmp_path, reject):
    sent = []

    def respond(request):
        sent.append(dict(request.headers))
        if reject == "rotate" and len(sent) == 1:
            auth_path = tmp_path / "auth.json"
            auth = json.loads(auth_path.read_text(encoding="utf-8-sig"))
            for entry in auth["credential_pool"]["anthropic"]:
                if entry["auth_type"] == "oauth":
                    entry["access_token"] = "sk-ant-oat-synthetic-rotated"
            auth_path.write_text(json.dumps(auth), encoding="utf-8")
        if reject is True or (reject == "rotate" and len(sent) == 1):
            return httpx.Response(401, json={"type": "error", "error": {
                "type": "authentication_error", "message": "invalid credential"}})
        return completion(json.loads(request.content)["model"])

    original = anthropic.Anthropic

    def sdk(**kwargs):
        kwargs["http_client"] = httpx.Client(transport=httpx.MockTransport(respond))
        kwargs["max_retries"] = 0
        return original(**kwargs)

    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.title_generator._auto_title_enabled", return_value=False), \
         patch("agent.turn_recovery.interruptible_backoff_sleep", return_value=None), \
         patch("anthropic.Anthropic", side_effect=sdk):
        agent = build_agent(session_id="billing-fence", max_iterations=3)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-paid-key")
        try:
            result = agent.run_conversation("Say ok")
            if reject is not True:
                assert result["final_response"] == "ok", result
            else:
                assert result.get("failed") or result.get("error"), result
            assert agent.model_routing["billing_group"] == "subscription"
        finally:
            agent.release_clients()
    assert sent
    assert all(h.get("authorization", "").startswith("Bearer sk-ant-oat") for h in sent)
    assert all("x-api-key" not in h for h in sent)
    if reject == "rotate":
        assert len(sent) == 2
        assert sent[-1]["authorization"] == "Bearer sk-ant-oat-synthetic-rotated"


@pytest.mark.parametrize("request_local", [False, True])
def test_invalid_subscription_credential_is_blocked_before_send(routing_home, streaming_route, request_local):
    from agent.model_routing import ModelRoutingError

    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        agent = build_agent(session_id="dispatch-guard")
        try:
            agent._anthropic_api_key = "synthetic-paid-key"
            with patch.object(agent, "_try_refresh_anthropic_client_credentials", return_value=False), \
                 patch("agent.anthropic_adapter.create_anthropic_message") as send:
                with pytest.raises(ModelRoutingError, match="OAuth token"):
                    if request_local:
                        agent._create_request_anthropic_client(reason="test")
                    else:
                        agent._anthropic_messages_create({"model": agent.model, "messages": []})
                send.assert_not_called()
        finally:
            agent.release_clients()


def test_new_turn_can_reuse_recovered_subscription(routing_home, streaming_route, tmp_path):
    routes = routing_home["model_routing"]["routes"]["deep"]
    second = deepcopy(routes[0])
    second["model"] = "z-anthropic-deep"
    paid = deepcopy(second)
    paid.update(model="paid-deep", billing="metered")
    routes.extend([second, paid])
    routing_home["model_routing"]["allow_metered"] = True
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        agent = build_agent(session_id="recovered-subscription")
        try:
            assert agent.model == "anthropic-deep"
            assert agent._try_activate_fallback()
            assert agent.model == "z-anthropic-deep"
            agent._restore_primary_runtime()
            assert agent.model == "z-anthropic-deep"  # no turn-start rebalance
            assert agent._try_activate_fallback()
            assert agent.model == "anthropic-deep"
            assert agent.model_routing["billing_group"] == "subscription"
        finally:
            agent.release_clients()
