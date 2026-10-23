"""Gateway context probes must not allocate or authenticate class markers."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.run import GatewayRunner, _resolve_gateway_model_context
from agent.model_routing import resolve_model_route, ModelRoutingError
from hermes_cli.config import atomic_config_replace
from tests.agent import test_model_routing_agent
from tests.tui_gateway import test_class_routing

native_transport = test_class_routing.native_transport

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route


@pytest.fixture(autouse=True)
def gateway_config_home(routing_home, monkeypatch):
    # Gateway's boot-time config path is imported before pytest binds the test home.
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda **kw: routing_home)


def test_default_context_probe_never_authenticates_or_allocates(routing_home, streaming_route, tmp_path):
    routing_home["model"] = {"default": "class:deep", "provider": "auto"}
    atomic_config_replace(tmp_path / "config.yaml", routing_home)
    with patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("legacy auth")) as legacy:
        context = _resolve_gateway_model_context()
    legacy.assert_not_called()
    assert context.context_length == routing_home["model_routing"]["routes"]["deep"][0]["capabilities"]["context_tokens"]
    assert context.context_source == "class-registry"
    assert not (tmp_path / "model-routing.sqlite3").exists()


@pytest.mark.asyncio
async def test_inbound_and_hygiene_use_sticky_context_without_reallocating(routing_home, streaming_route, tmp_path):
    import sqlite3
    first = routing_home["model_routing"]["routes"]["deep"][0]
    second = deepcopy(first)
    second["model"] = "z-smaller-context"
    second["capabilities"]["context_tokens"] = 120000
    routing_home["model_routing"]["routes"]["deep"].append(second)
    routing_home["model"] = {"default": "class:deep", "provider": "auto"}
    atomic_config_replace(tmp_path / "config.yaml", routing_home)
    resolve_model_route("class:deep", config=routing_home, session_id="s")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(_entries={"key": SimpleNamespace(session_id="s")})
    runner._resolve_session_agent_runtime = lambda **kw: ("class:deep", {"provider": "auto"})
    with sqlite3.connect(tmp_path / "model-routing.sqlite3") as db:
        counters = db.execute("SELECT * FROM counters").fetchall()
    with patch("agent.model_metadata.get_model_context_length_async", side_effect=AssertionError("marker catalog")):
        assert await runner._inbound_model_context_length(None, "key") == first["capabilities"]["context_tokens"]
        settings = await runner._hmwa_hygiene_settings(None, "key")
        assert settings.config_context_length == first["capabilities"]["context_tokens"]
        assert settings.model == "class:deep"
    with sqlite3.connect(tmp_path / "model-routing.sqlite3") as db:
        assert db.execute("SELECT * FROM counters").fetchall() == counters


def test_unallocated_context_is_conservative_and_revoked_sticky_fails(routing_home, streaming_route, tmp_path):
    from gateway.run_class_routing import class_context_length
    first = routing_home["model_routing"]["routes"]["deep"][0]
    second = deepcopy(first)
    second["model"] = "z-small"
    second["capabilities"]["context_tokens"] = 120000
    routing_home["model_routing"]["routes"]["deep"].append(second)
    assert class_context_length("class:deep", config=routing_home) == 120000
    assert not (tmp_path / "model-routing.sqlite3").exists()
    resolve_model_route("class:deep", config=routing_home, session_id="s")
    runner = SimpleNamespace(session_store=SimpleNamespace(_entries={"key": SimpleNamespace(session_id="s")}))
    first["eligible"] = False
    with pytest.raises(ModelRoutingError, match="No eligible class context"):
        class_context_length("class:deep", config=routing_home, runner=runner, session_key="key")


def test_empty_class_context_fails_without_default_window(routing_home, streaming_route, tmp_path):
    routing_home["model"] = {"default": "class:deep", "provider": "auto"}
    routing_home["model_routing"]["routes"]["deep"] = []
    atomic_config_replace(tmp_path / "config.yaml", routing_home)
    with pytest.raises(ModelRoutingError):
        _resolve_gateway_model_context()


@pytest.mark.asyncio
async def test_hygiene_marker_enters_detached_agent_without_preflight_key(
    routing_home, streaming_route, native_transport, tmp_path,
):
    from unittest.mock import AsyncMock
    from hermes_state import SessionDB, AsyncSessionDB

    routing_home["model"] = {"default": "class:deep", "provider": "auto"}
    atomic_config_replace(tmp_path / "config.yaml", routing_home)
    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session("s", source="local")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._session_db = AsyncSessionDB(db)
    entry = SimpleNamespace(session_id="s", last_prompt_tokens=1000000)
    runner.session_store = SimpleNamespace(_entries={"key": entry})
    runner._resolve_session_agent_runtime = lambda **kw: ("class:deep", {"provider": "auto"})
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    runner._event_thread_metadata = lambda *args: {}
    built = []

    async def detached(attempt, hs, plan, history, messages, model, runtime, *args):
        agent, _ = await runner._hmwa_hygiene_build_agent(model, runtime, entry)
        try:
            assert agent.model_routing["requested_class"] == "deep"
            assert agent.run_conversation("Say ok")["final_response"] == "ok"
            built.append(agent.model)
        finally:
            agent.release_clients()

    runner._hmwa_hygiene_detached_attempt = detached
    history = [{"role": role, "content": "old"} for role in ["user", "assistant", "user", "assistant"]]
    try:
        with patch("model_tools.get_tool_definitions", return_value=[]), \
                patch("agent.model_metadata.get_model_context_length_async", side_effect=AssertionError("marker catalog")):
            assert await runner._hmwa_run_session_hygiene(None, None, entry, "key", history, "key", 1) == history
        assert built == ["anthropic-deep"]
        assert native_transport
        assert all(body["model"] == "anthropic-deep" for _, _, body in native_transport)
    finally:
        db.close()
