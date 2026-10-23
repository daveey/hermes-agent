"""Messaging class requests defer credentials until durable agent construction."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from run_agent import AIAgent
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from gateway.session import Platform, SessionSource
from tests.gateway.test_pre_agent_fallback_notice import _runner_with_real_runtime_resolution
from tests.agent import test_model_routing_agent
from tests.tui_gateway import test_class_routing

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route
native_transport = test_class_routing.native_transport


def test_messaging_class_reaches_native_wire(routing_home, streaming_route, native_transport):
    runner = _runner_with_real_runtime_resolution()
    ctx = TurnContext(
        source=SessionSource(platform=Platform.LOCAL, chat_id="c", user_id="u"),
        message="hi", history=[], session_id="messaging-class", session_key="test-session-key",
        user_config=routing_home, AIAgent=AIAgent,
        resolve_display_setting=lambda *_a: False, _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
    )
    with patch("gateway.run._resolve_gateway_model", return_value="class:deep"), \
            patch("gateway.run._get_channel_override", return_value=None), \
            patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("legacy billing bypass")), \
            patch("model_tools.get_tool_definitions", return_value=[]):
        model, runtime = runner._resolve_session_agent_runtime(source=ctx.source, user_config=routing_home)
        agent = TurnRunner(runner, ctx)._build_fresh_agent(
            {"model": model, "runtime": runtime}, "local", "", 5, None, {}, True)
        assert agent.run_conversation("Say ok")["final_response"] == "ok"
        resumed = TurnRunner(runner, ctx)._build_fresh_agent(
            {"model": model, "runtime": runtime}, "local", "", 5, None, {}, True)
        assert resumed.model_routing == agent.model_routing
        agent.release_clients()
        resumed.release_clients()
    assert native_transport
    assert all(body["model"] == "anthropic-deep" and url.startswith("https://api.anthropic.com/")
               and headers.get("authorization", "").startswith("Bearer sk-ant-oat")
               for url, headers, body in native_transport)


@pytest.mark.parametrize("kind", ["channel", "session"])
def test_override_class_precedes_default_auth(kind, routing_home):
    runner = _runner_with_real_runtime_resolution()
    source = SessionSource(platform=Platform.LOCAL, chat_id="c")
    override = {"model": "class:deep", "provider": "auto"}
    channel = SimpleNamespace(**override) if kind == "channel" else None
    if kind == "session":
        runner._peek_session_state.return_value = SimpleNamespace(conversation=SimpleNamespace(model_override=override))
    with patch("gateway.run._resolve_gateway_model", return_value="paid-default"), \
            patch("gateway.run._get_channel_override", return_value=channel), \
            patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("legacy billing bypass")):
        model, runtime = runner._resolve_session_agent_runtime(source=source, user_config=routing_home)
    assert model == "class:deep"
    assert runtime == {"provider": "auto", "requested_provider": "auto"}


def test_resume_ignores_changed_defaults_but_explicit_pin_wins(routing_home, streaming_route, native_transport):
    from hermes_state import SessionDB
    from gateway.run_class_routing import stored_class
    runner = _runner_with_real_runtime_resolution()
    db = SessionDB()
    runner._session_db = db
    runner.session_store._entries = {"test-session-key": SimpleNamespace(session_id="stored-class")}
    with patch("model_tools.get_tool_definitions", return_value=[]):
        agent = AIAgent(model="class:deep", provider="auto", session_id="stored-class", session_db=db,
                        skip_context_files=True, skip_memory=True, quiet_mode=True)
        agent.run_conversation("Say ok")
    assert stored_class(runner, "test-session-key") == "class:deep"
    with patch("gateway.run._resolve_gateway_model", return_value="changed-default"), \
            patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("legacy billing bypass")):
        model, runtime = runner._resolve_session_agent_runtime(user_config=routing_home)
        assert model == "class:deep"
        assert runtime["provider"] == "auto"
        runner._peek_session_state.return_value = SimpleNamespace(conversation=SimpleNamespace(model_override={
            "model": "exact-user-pin", "provider": "anthropic", "api_key": "fake", "credential_pool": object()}))
        model, runtime = runner._resolve_session_agent_runtime(user_config=routing_home)
        assert model == "exact-user-pin"
    agent.release_clients()
    db.close()


def test_empty_messaging_pool_sends_nothing(routing_home, streaming_route, native_transport, monkeypatch):
    from agent.model_routing import ModelRoutingError
    runner = _runner_with_real_runtime_resolution()
    monkeypatch.setattr("agent.credential_pool.CredentialPool.has_available", lambda *a, **kw: False)
    with patch("gateway.run._resolve_gateway_model", return_value="class:deep"), \
            patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=AssertionError("legacy billing bypass")):
        model, runtime = runner._resolve_session_agent_runtime(user_config=routing_home)
        with pytest.raises(ModelRoutingError):
            AIAgent(model=model, **runtime, session_id="empty-messaging")
    assert not native_transport


def test_cached_class_never_adopts_legacy_fallback():
    from gateway.run_config_loaders import GatewayConfigLoadersMixin
    agent = SimpleNamespace(model_routing={"requested_class": "deep"}, _fallback_chain=[], _fallback_model=None)
    GatewayConfigLoadersMixin._apply_fallback_chain_to_agent(agent, [{"provider": "openrouter", "model": "paid"}])
    assert agent._fallback_chain == []
    assert agent._fallback_model is None
