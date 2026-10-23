"""Delegations allocate a new class job, never borrow a parent's paid transport."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.delegate_tool_config import _resolve_delegation_credentials
from tests.agent import test_model_routing_agent
from tests.tui_gateway import test_class_routing

routing_home = test_model_routing_agent.routing_home
streaming_route = test_model_routing_agent.streaming_route
native_transport = test_class_routing.native_transport


def test_class_preflight_defers_credentials():
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=AssertionError("premature allocation")):
        creds = _resolve_delegation_credentials({"model": "class:deep", "provider": "auto"}, SimpleNamespace())
    assert creds["model"] == "class:deep"
    assert creds["provider"] == "auto"
    assert creds["api_key"] is None


@pytest.mark.parametrize("explicit", [False, True])
def test_delegated_class_reaches_native_wire(routing_home, streaming_route, native_transport, explicit):
    from run_agent import AIAgent
    from tools.delegate_tool import _build_child_agent, _credential_overrides
    with patch("model_tools.get_tool_definitions", return_value=[]), \
            patch("tools.delegate_tool._load_config", return_value={}):
        parent = AIAgent(model="class:deep", provider="auto", session_id="parent-class",
                         quiet_mode=True, skip_memory=True, skip_context_files=True)
        cfg = {"model": "class:deep", "provider": "auto"} if explicit else {}
        creds = _resolve_delegation_credentials(cfg, parent)
        child = _build_child_agent(task_index=0, goal="Say ok", context=None, toolsets=None,
                                   model=creds["model"], max_iterations=2, task_count=1,
                                   parent_agent=parent, **_credential_overrides(creds, cfg))
        assert child.model_routing["requested_class"] == "deep"
        assert child.session_id != parent.session_id
        assert child._credential_pool.current().auth_type == "oauth"
        assert child._fallback_chain == []
        assert child.run_conversation("Say ok")["final_response"] == "ok"
        child.release_clients()
        parent.release_clients()
    assert native_transport
    assert all(body["model"] == "anthropic-deep" and url.startswith("https://api.anthropic.com/")
               and headers.get("authorization", "").startswith("Bearer sk-ant-oat")
               for url, headers, body in native_transport)


@pytest.mark.parametrize("override", [{"provider": "anthropic"}, {"base_url": "https://paid.invalid"},
                                     {"api_key": "paid-key"}, {"command": "some-command"}])
def test_class_rejects_transport_overrides(override):
    from agent.model_routing import ModelRoutingError
    with pytest.raises(ModelRoutingError):
        _resolve_delegation_credentials({"model": "class:deep", **override}, SimpleNamespace())


def test_explicit_model_does_not_inherit_class():
    parent = SimpleNamespace(model_routing={"requested_class": "deep"}, request_overrides={})
    creds = _resolve_delegation_credentials({"model": "exact-user-pin"}, parent)
    assert creds["model"] == "exact-user-pin"
    assert creds["provider"] is None


def test_exhausted_child_does_not_send_or_inherit_paid_fallback(routing_home, streaming_route,
                                                              native_transport, monkeypatch):
    from agent.model_routing import ModelRoutingError
    from run_agent import AIAgent
    from tools.delegate_tool import _build_child_agent, _credential_overrides
    with patch("model_tools.get_tool_definitions", return_value=[]), \
            patch("tools.delegate_tool._load_config", return_value={}):
        parent = AIAgent(model="class:deep", provider="auto", session_id="parent-empty",
                         quiet_mode=True, skip_memory=True, skip_context_files=True)
        parent._fallback_chain = [{"provider": "openrouter", "model": "paid"}]
        monkeypatch.setattr("agent.credential_pool.CredentialPool.has_available", lambda *a, **kw: False)
        cfg = {"model": "class:deep", "provider": "auto", "fallback_providers": parent._fallback_chain}
        creds = _resolve_delegation_credentials(cfg, parent)
        with pytest.raises(ModelRoutingError):
            _build_child_agent(task_index=0, goal="Say ok", context=None, toolsets=None,
                               model=creds["model"], max_iterations=2, task_count=1,
                               parent_agent=parent, **_credential_overrides(creds, cfg))
        parent.release_clients()
    assert not native_transport
