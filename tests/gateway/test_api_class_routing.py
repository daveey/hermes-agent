"""API class requests must not authenticate the legacy default first."""
from unittest.mock import patch

import pytest
import run_agent
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from tests.agent import test_model_routing_agent
from tests.tui_gateway import test_class_routing

native_transport = test_class_routing.native_transport
routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    api = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(api, "_session_model_override_for", lambda key: None)
    monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", lambda *a: [])
    return api


def test_api_constructor_class_wire_and_resume(adapter, routing_home, streaming_route, native_transport):
    with patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("paid default")), \
            patch("model_tools.get_tool_definitions", return_value=[]):
        agent = adapter._create_agent(session_id="api-class", requested_model="class:deep", requested_provider="auto")
        assert isinstance(agent, run_agent.AIAgent)
        assert agent.run_conversation("Say ok")["final_response"] == "ok"
        assert agent.model_routing["requested_class"] == "deep"
        assert agent._hermes_api_runtime["provider"] == "anthropic"
        assert agent._hermes_api_runtime["model"] == "anthropic-deep"
        resumed = adapter._create_agent(session_id="api-class", session_model=agent.model)
        assert resumed.model_routing == agent.model_routing
        assert resumed.model == agent.model
        agent.release_clients()
        resumed.release_clients()
    assert native_transport
    assert all(body["model"] == "anthropic-deep" and url.startswith("https://api.anthropic.com/")
               and headers.get("authorization", "").startswith("Bearer sk-ant-oat")
               and "x-api-key" not in headers for url, headers, body in native_transport)


def test_api_empty_pool_never_uses_legacy(adapter, routing_home, streaming_route, monkeypatch):
    from agent.model_routing import ModelRoutingError
    monkeypatch.setattr("agent.credential_pool.CredentialPool.has_available", lambda *a, **kw: False)
    with patch("gateway.run._resolve_runtime_agent_kwargs") as legacy, patch("anthropic.Anthropic") as sdk:
        with pytest.raises(ModelRoutingError):
            adapter._create_agent(session_id="empty", requested_model="class:deep", requested_provider="auto")
    legacy.assert_not_called()
    sdk.assert_not_called()


@pytest.mark.parametrize("selection", ["global", "pin", "incompatible"])
def test_api_class_default_and_explicit_selection(adapter, routing_home, streaming_route, native_transport,
                                                  monkeypatch, tmp_path, selection):
    from hermes_cli.config import atomic_config_write
    from agent.model_routing import ModelRoutingError
    routing_home["model"].update(default="class:deep", provider="auto")
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "class:deep")
    kwargs = {"session_id": "selection-" + selection}
    if selection == "pin":
        kwargs.update(requested_model="anthropic-deep", requested_provider="anthropic")
    elif selection == "incompatible":
        kwargs.update(requested_model="class:deep", requested_provider="openrouter")
    with patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("paid default")), \
            patch("model_tools.get_tool_definitions", return_value=[]):
        if selection == "incompatible":
            with pytest.raises(ModelRoutingError):
                adapter._create_agent(**kwargs)
            assert not native_transport
            return
        agent = adapter._create_agent(**kwargs)
        assert agent.model == "anthropic-deep"
        assert bool(getattr(agent, "model_routing", None)) is (selection == "global")
        assert agent.run_conversation("Say ok")["final_response"] == "ok"
        agent.release_clients()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_completions_http_class(adapter, routing_home, streaming_route, native_transport, stream):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    with patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("paid default")), \
            patch("model_tools.get_tool_definitions", return_value=[]):
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/v1/chat/completions", json={
                "model": "class:deep", "provider": "auto", "stream": stream,
                "messages": [{"role": "user", "content": "Say ok"}]})
            text = await response.text()
            assert response.status == 200, text
            assert "ok" in text
    assert native_transport
    assert all(body["model"] == "anthropic-deep" and headers.get("authorization", "").startswith("Bearer sk-ant-oat")
               for _, headers, body in native_transport)


@pytest.mark.asyncio
async def test_runs_http_class(adapter, routing_home, streaming_route, native_transport):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from tests.waiting import async_wait_until
    app = web.Application()
    app.router.add_post("/v1/runs", adapter._handle_runs)
    with patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("paid default")), \
            patch("model_tools.get_tool_definitions", return_value=[]):
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/v1/runs", json={
                "model": "class:deep", "provider": "auto", "input": "Say ok"})
            payload = await response.json()
            assert response.status == 202, payload
            await async_wait_until(lambda: not adapter.active_agent_work_count(), timeout=20)
            status = adapter._run_statuses[payload["run_id"]]
            assert status["status"] == "completed", status
            assert status["output"] == "ok"
            assert status["runtime"]["provider"] == "anthropic"
            assert status["runtime"]["model"] == "anthropic-deep"
    assert native_transport
    assert all(body["model"] == "anthropic-deep" and headers.get("authorization", "").startswith("Bearer sk-ant-oat")
               for _, headers, body in native_transport)
