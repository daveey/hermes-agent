"""Class markers resolve at the common agent boundary before client creation."""
from unittest.mock import MagicMock, patch

import pytest

from agent.model_routing import ModelRoutingError
from run_agent import AIAgent
from tests.hermes_cli import test_model_routing_runtime

routing_home = test_model_routing_runtime.routing_home


@pytest.fixture(autouse=True)
def streaming_route(routing_home, tmp_path):
    from hermes_cli.config import atomic_config_write
    routing_home["model"] = {"context_length": 100000}
    routing_home["model_routing"]["routes"]["deep"][0]["capabilities"]["streaming"] = True
    atomic_config_write(tmp_path / "config.yaml", routing_home)


def build_agent(**kwargs):
    return AIAgent(model="class:deep", provider="auto", quiet_mode=True,
                   skip_context_files=True, skip_memory=True, **kwargs)


def test_agent_resolves_class_before_native_client_and_keeps_resume(routing_home):
    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()) as client:
        first = build_agent(session_id="durable", fallback_model=[
            {"provider": "openrouter", "model": "unapproved-paid"}])
        resumed = build_agent(session_id="durable")
    assert first.model == resumed.model == "anthropic-deep"
    assert first.provider == resumed.provider == "anthropic"
    assert first.model_routing == resumed.model_routing
    assert first.model_routing["requested_class"] == "deep"
    assert first._credential_pool.current().auth_type == "oauth"
    assert first._fallback_chain == []
    assert client.call_count == 2
    assert client.call_args.args[0].startswith("sk-ant-oat")


def test_agent_allocates_identity_before_routing(routing_home):
    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        agent = build_agent()
    assert agent.session_id
    assert agent.model_routing["selected_model"] == agent.model


def test_agent_class_rejects_endpoint_override_without_client(routing_home):
    with patch("agent.anthropic_adapter.build_anthropic_client") as client:
        with pytest.raises(ModelRoutingError):
            build_agent(session_id="blocked", base_url="https://paid.example.test")
    client.assert_not_called()


@pytest.mark.parametrize("client_failure", [False, True])
def test_agent_class_failure_stays_same_class_and_preserves_prompt(routing_home, tmp_path, client_failure):
    from copy import deepcopy
    from hermes_cli.config import atomic_config_write
    second = deepcopy(routing_home["model_routing"]["routes"]["deep"][0])
    second["model"] = "z-anthropic-deep"
    routing_home["model_routing"]["routes"]["deep"].append(second)
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        agent = build_agent(session_id="failover")
        agent._cached_system_prompt = "byte-stable prompt"
        if client_failure:
            with patch("agent.anthropic_adapter.build_anthropic_client", side_effect=RuntimeError("unavailable")):
                with pytest.raises(ModelRoutingError, match="client construction"):
                    agent._try_activate_fallback()
            assert agent.model == "anthropic-deep"
            assert agent.model_routing["selected_model"] == agent.model
            return
        assert agent._try_activate_fallback() is True
        assert agent.model == "z-anthropic-deep"
        assert agent._cached_system_prompt == "byte-stable prompt"
        assert agent.model_routing["reason"] == "same-class-failover"
        assert agent._credential_pool.current().auth_type == "oauth"
        resumed = build_agent(session_id="failover")
        assert resumed.model == agent.model
        assert agent._try_activate_fallback() is False


def test_class_capability_and_context_rejection_never_builds_client(routing_home, tmp_path):
    from hermes_cli.config import atomic_config_write
    routing_home["model"]["context_length"] = 900000
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    with patch("agent.anthropic_adapter.build_anthropic_client") as client:
        with pytest.raises(ModelRoutingError):
            build_agent(session_id="too-large")
    client.assert_not_called()


def test_explicit_switch_clears_class_policy(routing_home):
    with patch("model_tools.get_tool_definitions", return_value=[]), \
         patch("agent.anthropic_adapter.build_anthropic_client", return_value=MagicMock()):
        agent = build_agent(session_id="switch")
        agent.switch_model("explicit-user-model", "anthropic", api_key=agent.api_key,
                           base_url=agent.base_url, api_mode="anthropic_messages")
    assert agent.model == "explicit-user-model"
    assert agent.model_routing is None


def test_agent_empty_class_fails_before_legacy_paid_fallback(routing_home, monkeypatch):
    monkeypatch.setattr("agent.credential_pool.CredentialPool.has_available", lambda *a, **k: False)
    with patch("agent.auxiliary_client.resolve_provider_client") as legacy:
        with pytest.raises(ModelRoutingError):
            build_agent(session_id="empty", fallback_model=[
                {"provider": "openrouter", "model": "paid"}])
    legacy.assert_not_called()
