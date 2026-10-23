"""Desktop construction and resume preserve the class credential fence."""
from unittest.mock import patch

import pytest
import run_agent  # bootstrap before real-home guard
from tui_gateway import server
from tests.agent import test_model_routing_agent

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route


@pytest.fixture
def native_transport(monkeypatch):
    import json
    import anthropic
    import httpx
    sent = []

    def respond(request):
        sent.append((str(request.url), dict(request.headers), json.loads(request.content)))
        events = [
            {"type": "message_start", "message": {
                "id": "test", "type": "message", "role": "assistant", "model": "anthropic-deep",
                "content": [], "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
            {"type": "message_stop"}]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    original = anthropic.Anthropic

    def client(**kwargs):
        return original(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))

    monkeypatch.setattr(anthropic, "Anthropic", client)
    monkeypatch.setattr("agent.title_generator._auto_title_enabled", lambda: False)
    return sent


def test_desktop_construct_resume_and_pin(routing_home, streaming_route, native_transport, monkeypatch):
    from hermes_cli.config import load_config
    monkeypatch.setattr(server, "_load_cfg", load_config)
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("class:deep", "auto"))
    monkeypatch.setattr(server, "_agent_cbs", lambda sid: {})
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform: [])
    monkeypatch.setattr(server, "_startup_system_prompt", lambda *a: "")
    monkeypatch.setattr(server, "_get_db", lambda: None)
    with patch("model_tools.get_tool_definitions", return_value=[]):
        agent = server._make_agent("ui-id", "durable-id")
        assert isinstance(agent, run_agent.AIAgent)
        assert agent.model == "anthropic-deep"
        assert agent.model_routing["requested_class"] == "deep"
        assert agent.run_conversation("Say ok")["final_response"] == "ok"
        assert native_transport
        assert all(body["model"] == "anthropic-deep" for _, _, body in native_transport)
        assert all(url.startswith("https://api.anthropic.com/") for url, _, _ in native_transport)
        assert all(headers.get("authorization", "").startswith("Bearer sk-ant-oat")
                   and "x-api-key" not in headers for _, headers, _ in native_transport)
        config = server._runtime_model_config(agent)
        overrides = server._stored_session_runtime_overrides({"model": agent.model, "model_config": config})
        assert overrides["model_override"]["model"] == "class:deep"
        assert overrides["provider_override"] == "auto"
        resumed = server._make_agent("new-ui-id", "durable-id", **overrides)
        assert resumed.model_routing == agent.model_routing
        monkeypatch.setattr(server, "_config_model_target", lambda: ("class:standard", ""))
        session = {"agent": agent, "model_override": {"model": "class:deep"},
                   "composer_override_profile": {"model": "old", "provider": "auto"}}
        with patch.object(server, "_apply_model_switch") as switch:
            server._sync_agent_model_with_config("ui-id", session)
        switch.assert_not_called()
        assert session["model_override"] == {"model": "class:deep"}
        agent.switch_model(agent.model, agent.provider, api_key=agent.api_key,
                           base_url=agent.base_url, api_mode=agent.api_mode)
        assert "model_routing" not in server._runtime_model_config(agent, config)
        agent.release_clients()
        resumed.release_clients()


def test_desktop_exhaustion_cannot_enter_paid_fallback(routing_home, streaming_route, monkeypatch):
    from agent.model_routing import ModelRoutingError
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("class:deep", "auto"))
    monkeypatch.setattr(server, "_agent_cbs", lambda sid: {})
    monkeypatch.setattr(server, "_startup_system_prompt", lambda *a: "")
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr("agent.credential_pool.CredentialPool.has_available", lambda *a, **k: False)
    with patch("anthropic.Anthropic") as sdk, patch.object(server, "_resolve_runtime_with_fallback") as legacy:
        with pytest.raises(ModelRoutingError):
            server._make_agent("ui-empty", "durable-empty")
    sdk.assert_not_called()
    legacy.assert_not_called()
