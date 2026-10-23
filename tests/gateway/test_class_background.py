"""Background messaging work resolves marker-only runtimes at agent creation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.run import GatewayRunner
from gateway.config import Platform
from gateway.session import SessionSource
from hermes_cli.config import atomic_config_replace
from tests.agent import test_model_routing_agent
from tests.tui_gateway import test_class_routing

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route
native_transport = test_class_routing.native_transport


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
async def test_background_class_native_wire(routing_home, streaming_route, native_transport, tmp_path, empty):
    routing_home["model"] = {"default": "class:deep", "provider": "auto"}
    if empty:
        routing_home["model_routing"]["routes"]["deep"] = []
    atomic_config_replace(tmp_path / "config.yaml", routing_home)
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = SimpleNamespace(send=AsyncMock(), emit_warning=AsyncMock(),
                              extract_media=lambda text: ([], text), extract_images=lambda text: ([], text))
    runner._delivery_adapter_for = lambda source: adapter
    runner._thread_metadata_for_source = lambda *args: {}
    runner._resolve_session_agent_runtime = lambda **kw: ("class:deep", {"provider": "auto"})
    runner._resolve_turn_toolsets = lambda *args: ([], None)
    runner._provider_routing = {}
    runner._resolve_session_reasoning_config = lambda **kw: None
    runner._resolve_session_service_tier = lambda **kw: None
    runner._resolve_turn_agent_config = lambda prompt, model, runtime: {"model": model, "runtime": runtime}
    runner._session_db = None
    runner._refresh_fallback_model = lambda: [{"model": "paid", "provider": "openrouter"}]
    runner._cleanup_agent_resources = lambda agent: agent.release_clients()

    async def execute(fn):
        return fn()

    runner._run_in_executor_with_context = execute
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="fixture")
    with patch("gateway.run._load_gateway_config", return_value=routing_home), \
            patch("gateway.run._current_max_iterations", return_value=3), \
            patch("model_tools.get_tool_definitions", return_value=[]):
        await runner._run_background_task_inner("Say ok", source, "background-class")
    if empty:
        assert native_transport == []
        adapter.emit_warning.assert_awaited_once()
    else:
        adapter.emit_warning.assert_not_awaited()
        assert native_transport
        assert all(body["model"] == "anthropic-deep" for _, _, body in native_transport)
        assert "ok" in adapter.send.call_args.kwargs["content"]
