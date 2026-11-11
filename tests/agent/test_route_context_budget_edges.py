"""Adversarial controls for route pressure: switches, plugin engines and output-only overflow."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.error_classifier import classify_api_error
from agent.model_routing_request import output_reservation, route_preflight_pressure
from tests.agent import test_route_context_recovery as recovery
from tests.agent.test_context_engine import StubEngine

route = recovery.route


def test_runtime_switch_drops_old_route_ceiling(route):
    agent, _, _ = route
    engine = agent.context_compressor = ContextCompressor(
        agent.model, provider=agent.provider, config_context_length=1050000, threshold_percent=.85,
        max_tokens=8000, quiet_mode=True)
    route_preflight_pressure(agent, {}, 10)
    assert engine.threshold_tokens == 258560
    engine.update_model("another-model", context_length=1000000, provider="another-provider")
    assert engine.threshold_tokens == 843200


def test_same_runtime_recalibration_retains_aux_and_config_caps(route):
    agent, _, _ = route
    engine = agent.context_compressor = ContextCompressor(
        agent.model, provider=agent.provider, config_context_length=1050000, threshold_percent=.85,
        max_tokens=8000, threshold_tokens_cap=500000, quiet_mode=True)
    engine._aux_context_ceiling = 400000
    route_preflight_pressure(agent, {}, 10)
    engine.update_model(agent.model, context_length=1050000, provider=agent.provider)
    assert engine.threshold_tokens == 258560
    agent.model_routing = None
    route_preflight_pressure(agent, {}, 10)
    assert engine.threshold_tokens == 400000
    engine._aux_context_ceiling = None
    engine.update_model(agent.model, context_length=1050000, provider=agent.provider)
    assert engine.threshold_tokens == 500000


def test_plugin_engine_honors_cap_and_reset(route):
    agent, _, _ = route
    engine = agent.context_compressor = StubEngine(context_length=1050000)
    route_preflight_pressure(agent, {}, 10)
    assert engine.threshold_tokens == 258560
    assert engine.should_compress(268000)
    engine.update_model(context_length=2000000)
    assert engine.threshold_tokens == 400000


@pytest.mark.parametrize("reservation", [272000, 300000])
def test_output_only_overflow_does_not_destroy_history(route, reservation):
    agent, _, _ = route
    agent.context_compressor = ContextCompressor("test", config_context_length=1050000, quiet_mode=True)
    request = {"max_tokens": reservation}
    before = agent.context_compressor.threshold_tokens
    assert route_preflight_pressure(agent, request, 100) == 100
    assert agent.context_compressor.threshold_tokens == before
    assert not agent._route_context_pressure
    assert not classify_api_error(recovery.refusal(agent, **request)).should_compress


def test_reservation_respects_ephemeral_and_explicit_wire_values():
    agent = SimpleNamespace(max_tokens=8000, _ephemeral_max_output_tokens=16000)
    assert output_reservation(agent, {}) == 16000
    assert agent._ephemeral_max_output_tokens == 16000  # preview must not consume it
    assert output_reservation(agent, {"max_output_tokens": 12000}) == 12000
    agent._ephemeral_max_output_tokens = None
    assert output_reservation(agent, {}) == 8000
    agent.max_tokens = None
    assert output_reservation(agent, {}) == 4096


def test_anthropic_default_matches_adapter():
    from agent.anthropic_adapter import _resolve_anthropic_messages_max_tokens
    agent = SimpleNamespace(api_mode="anthropic_messages", model="claude-sonnet-5-5", max_tokens=None,
                            context_compressor=SimpleNamespace(context_length=1000000))
    assert output_reservation(agent, {}) == _resolve_anthropic_messages_max_tokens(None, agent.model)
    agent.context_compressor.context_length = 64000
    with patch("agent.anthropic_adapter._resolve_anthropic_messages_max_tokens", return_value=128000):
        assert output_reservation(agent, {}) == 63999
