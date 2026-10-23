"""Implicit auxiliary jobs inherit class policy, never a paid legacy ladder."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import auxiliary_client as aux
from agent import auxiliary_class_routing as routing
from tests.tui_gateway import test_class_routing as fixtures

routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route
native_transport = fixtures.native_transport


@pytest.mark.parametrize("source", ["explicit-runtime", "turn-context", "config"])
def test_auto_auxiliary_inherits_class_on_wire(source, routing_home, streaming_route,
                                               native_transport, monkeypatch, tmp_path):
    from hermes_cli.config import atomic_config_write
    from agent.turn_context import _publish_runtime_main
    from run_agent import AIAgent

    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})
    aux.clear_runtime_main()
    options = {}
    agent = SimpleNamespace(model="anthropic-deep", provider="anthropic", session_id="owner",
                            model_routing={"requested_class": "deep"})
    if source == "explicit-runtime":
        options["main_runtime"] = AIAgent._current_main_runtime(agent)
    elif source == "turn-context":
        _publish_runtime_main(agent)
    else:
        routing_home["model"].update(default="class:deep", provider="auto")
        atomic_config_write(tmp_path / "config.yaml", routing_home)
    try:
        with patch.object(aux, "_try_discovery_chain", side_effect=AssertionError("legacy discovery")), \
                patch.object(aux, "_try_main_provider_route", side_effect=AssertionError("unfenced route")):
            response = aux.call_llm("title", messages=[{"role": "user", "content": "ok"}], **options)
        assert response.choices[0].message.content == "ok"
        assert native_transport[0][2]["model"] == "anthropic-deep"
        assert native_transport[0][1]["authorization"].startswith("Bearer sk-ant-oat")
    finally:
        aux.clear_runtime_main()


@pytest.mark.parametrize("pin", [{"model": "exact-user-model"}, {"provider": "custom"},
                                 {"base_url": "https://pinned.invalid"}, {"api_key": "explicit"}])
def test_explicit_auxiliary_pin_does_not_inherit_class(pin, monkeypatch):
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})
    options = {"main_runtime": {"requested_class": "deep", "model": "selected", "provider": "anthropic"}, **pin}
    assert routing._class_request("title", options) is None


def test_concrete_main_runtime_wins_over_class_config(monkeypatch):
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})
    monkeypatch.setattr(aux, "_read_main_model", lambda: "class:deep")
    assert routing._class_request("title", {"main_runtime": {"model": "exact-user-model"}}) is None


def test_explicit_class_wins_over_inherited_class(monkeypatch):
    monkeypatch.setattr(aux, "_get_auxiliary_task_config", lambda task: {})
    request = routing._class_request("title", {"model": "class:fast", "main_runtime": {
        "requested_class": "deep", "model": "selected", "session_id": "owner"}})
    assert request[0] == "class:fast"
