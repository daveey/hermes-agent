"""Main-loop physical attempts cannot outgrow or override their class route."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.model_routing import ModelRoutingError
from agent.turn_api_call import perform_api_call
from tests.agent.test_model_routing_agent import build_agent
from tests.tui_gateway import test_class_routing

routing_home = test_class_routing.routing_home
streaming_route = test_class_routing.streaming_route
native_transport = test_class_routing.native_transport


def send(agent, kwargs):
    return perform_api_call(
        agent, api_kwargs=kwargs, _original_api_kwargs=deepcopy(kwargs),
        _llm_middleware_trace=[], _moa_prepared_request=None, _retry=SimpleNamespace(),
        thinking_spinner=None, retry_count=0, api_call_count=0, api_request_id="request",
        effective_task_id="task", turn_id="turn", interrupted=False)


@pytest.fixture
def agent(routing_home, streaming_route, native_transport, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from hermes_cli.config import atomic_config_write
    routing_home["model_routing"]["routes"]["deep"][0]["capabilities"]["vision"] = False
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    with patch("model_tools.get_tool_definitions", return_value=[]):
        instance = build_agent(session_id="guard")
    yield instance
    instance.release_clients()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("mutation", ["model", "extra_model", "auth", "vision", "context", "json"])
def test_incompatible_attempt_never_reaches_native_wire(agent, native_transport, mutation, streaming):
    agent._disable_streaming = not streaming
    kwargs = agent._build_api_kwargs([{"role": "user", "content": "Say ok"}])
    mutations = {
        "model": {"model": "class:fast"},
        "extra_model": {"extra_body": {"model": "unapproved-paid"}},
        "auth": {"extra_headers": {"Authorization": "Bearer unapproved-paid"}},
        "vision": {"messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.test/photo.png"}}]}]},
        "context": {"messages": [{"role": "user", "content": "x" * 500000}]},
        "json": {"response_format": {"type": "json_object"}},
    }
    kwargs.update(mutations[mutation])
    with pytest.raises(ModelRoutingError):
        send(agent, kwargs)
    assert native_transport == []


def test_class_image_rejection_does_not_silently_drop_input(agent, native_transport):
    from agent.turn_api_request import build_api_request
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.test/photo.png"}}]}]
    agent._image_rejecting_models.add((agent.provider, agent.model))
    before = deepcopy(messages)
    with pytest.raises(ModelRoutingError):
        build_api_request(
            agent, api_messages=messages, _moa_prepared_request=None,
            tools_for_api=agent.tools, system_message="", messages=messages,
            original_user_message="photo", approx_tokens=100, total_chars=100,
            retry_count=0, api_call_count=0, api_request_id="request", api_start_time=0,
            effective_task_id="task", turn_id="turn")
    assert native_transport == []
    assert messages == before


def test_execution_middleware_cannot_override_selected_model(agent, native_transport):
    def mutate(request, next_call, **kwargs):
        return next_call(dict(request, model="paid-bypass"))
    with patch("hermes_cli.middleware.run_llm_execution_middleware", side_effect=mutate):
        with pytest.raises(ModelRoutingError):
            send(agent, agent._build_api_kwargs([{"role": "user", "content": "Say ok"}]))
    assert native_transport == []


def test_explicit_switch_does_not_apply_class_policy(agent, routing_home, tmp_path, native_transport):
    from hermes_cli.config import atomic_config_write
    agent.switch_model(agent.model, agent.provider, api_key=agent.api_key,
                       base_url=agent.base_url, api_mode=agent.api_mode)
    routing_home["model_routing"]["enabled"] = False
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    assert send(agent, agent._build_api_kwargs([{"role": "user", "content": "Say ok"}])).action == "fallthrough"
    assert len(native_transport) == 1


def test_repeated_attempts_stay_sticky_without_mutating_payload(agent, native_transport):
    kwargs = agent._build_api_kwargs([{"role": "user", "content": "Say ok"}])
    before = deepcopy(kwargs)
    metadata = deepcopy(agent.model_routing)
    for _ in range(2):
        assert send(agent, deepcopy(kwargs)).action == "fallthrough"
    assert kwargs == before
    assert agent.model_routing == metadata
    assert len(native_transport) == 2
    assert all(body["model"] == agent.model for _, _, body in native_transport)
    assert all(headers.get("authorization", "").startswith("Bearer sk-ant-oat")
               for _, headers, _ in native_transport)


def test_withdrawn_eligibility_fails_without_reallocating(agent, routing_home, tmp_path, native_transport):
    from hermes_cli.config import atomic_config_write
    routing_home["model_routing"]["routes"]["deep"][0]["eligible"] = False
    atomic_config_write(tmp_path / "config.yaml", routing_home)
    with pytest.raises(ModelRoutingError):
        send(agent, agent._build_api_kwargs([{"role": "user", "content": "Say ok"}]))
    assert native_transport == []
