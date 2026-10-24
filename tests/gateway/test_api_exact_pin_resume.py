"""API resume retains the exact provider through runtime precedence and construction."""
from copy import deepcopy
from unittest.mock import patch

from agent.model_routing_state import RoutingState
from hermes_cli.config import atomic_config_write
from hermes_cli.runtime_provider import resolve_runtime_provider
from tests.gateway import test_api_class_routing as fixtures

adapter = fixtures.adapter
native_transport = fixtures.native_transport
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


def test_api_exact_pin_resume_without_allocation_db(
        adapter, routing_home, streaming_route, native_transport, tmp_path):
    routing_home["model_routing"]["enforce_subscription_only"] = True
    second = deepcopy(routing_home["model_routing"]["routes"]["deep"][0])
    second["model"] = "z-exact-model"
    routing_home["model_routing"]["routes"]["deep"].append(second)
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    original = resolve_runtime_provider(requested="anthropic", target_model=second["model"],
                                        routing_session_id="api-pin")
    db = adapter._ensure_session_db()
    db.create_session("api-pin", source="api_server", model=second["model"], model_config={
        "model_routing": original["model_routing"], "provider": "openrouter"})
    path = tmp_path / "model-routing.sqlite3"
    path.unlink()
    with patch("gateway.run._resolve_gateway_model", return_value="paid-default"), patch(
        "gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("paid default")), patch(
        "model_tools.get_tool_definitions", return_value=[]):
        agent = adapter._create_agent(session_id="api-pin", session_model=second["model"],
                                       requested_model="ignored", requested_provider="openrouter")
        try:
            assert (agent.model, agent.provider) == (second["model"], "anthropic")
            assert agent.model_routing["exact_pin"] is True
            assert RoutingState(path).peek("api-pin", "deep") == agent.model_routing
            assert agent._fallback_chain == []
            assert agent.run_conversation("Say ok")["final_response"] == "ok"
            assert agent._try_activate_fallback() is False
        finally:
            agent.release_clients()
    assert native_transport
    assert all(body["model"] == second["model"] and headers.get("authorization", "").startswith("Bearer sk-ant-oat")
               and "x-api-key" not in headers for _, headers, body in native_transport)
