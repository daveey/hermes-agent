"""Keep routed preflight in the validator's units and reserve output runway."""
from types import SimpleNamespace
from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from tests.agent import test_route_context_recovery as recovery
from tests.agent import test_compression_feasibility as feasibility

route = recovery.route


def test_attested_budget_caps_larger_metadata_window(route, caplog):
    from agent.model_routing_request import route_preflight_pressure
    agent, _, _ = route
    compressor = agent.context_compressor = ContextCompressor(
        "gpt-6-astra", provider="openai-codex", config_context_length=1050000,
        threshold_percent=.85, max_tokens=8000, quiet_mode=True)
    request = {"messages": [{"role": "user", "content": "x" * 1072000}], "max_tokens": 8000}
    with caplog.at_level("INFO"):
        pressure = route_preflight_pressure(agent, request, 10000)
        route_preflight_pressure(agent, request, 10000)
    assert pressure >= 268000
    assert compressor.threshold_tokens + 8000 <= 272000 * .98
    assert agent._route_context_pressure
    assert sum("Route context cap" in r.message for r in caplog.records) == 1
    compressor.update_model("gpt-6-astra", context_length=1050000, provider="openai-codex")
    assert compressor.threshold_tokens + 8000 <= 272000 * .98


def test_budget_uses_request_reservation_and_updates_each_request(route):
    from agent.model_routing_request import route_preflight_pressure
    agent, _, _ = route
    agent.context_compressor = ContextCompressor("test", config_context_length=1050000, quiet_mode=True)
    route_preflight_pressure(agent, {"messages": [], "max_output_tokens": 16000}, 10)
    assert agent.context_compressor.threshold_tokens == 250560
    route_preflight_pressure(agent, {"messages": [], "max_completion_tokens": 8000}, 10)
    assert agent.context_compressor.threshold_tokens == 258560
    assert not agent._route_context_pressure


def test_unrouted_pressure_unchanged():
    from agent.model_routing_request import route_preflight_pressure
    agent = SimpleNamespace()
    assert route_preflight_pressure(agent, {}, 123) == 123


def test_autoraise_correct_codex_window_is_already_safe():
    # Falsifies the proposed autoraise-only cause: the observed bug needs either
    # estimator divergence or a larger metadata window, not .85 against 272k.
    from agent.agent_init import _resolve_compression_threshold
    from agent.auxiliary_client import _compression_threshold_for_model
    threshold, _ = _resolve_compression_threshold(
        .50, _compression_threshold_for_model("gpt-6-astra", "openai-codex"),
        model="gpt-6-astra", is_codex_autoraise=True)
    compressor = ContextCompressor("gpt-6-astra", provider="openai-codex",
                                   config_context_length=272000, threshold_percent=threshold,
                                   max_tokens=8000, quiet_mode=True)
    assert compressor.threshold_tokens + 8000 <= 272000 * .98


def test_proactive_route_budget_compacts_before_first_send(route, monkeypatch):
    from agent.model_routing_request import validate_class_request
    from tests.agent import test_413_compression as loop
    agent = loop._new_test_agent()
    selected, _, _entry = route
    agent.model, agent.provider = selected.model, selected.provider
    agent.model_routing = selected.model_routing
    agent._model_routing_requirements = {}
    agent.max_tokens = 8000
    agent.context_compressor = ContextCompressor(
        agent.model, provider=agent.provider, config_context_length=1050000,
        threshold_percent=.85, max_tokens=8000, quiet_mode=True)
    monkeypatch.setattr(agent, "_check_compression_model_feasibility", lambda: None)
    monkeypatch.setattr("agent.conversation_loop._midturn_request_pressure_tokens", lambda *a: 10000)
    sent = []
    def send(**kwargs):
        validate_class_request(agent, kwargs, streaming=True)
        sent.append(kwargs)
        return loop._mock_response("Recovered")
    agent.client.chat.completions.create.side_effect = send
    history = [{"role": "user", "content": "x" * 1072000},
               {"role": "assistant", "content": "working"}]
    with patch.object(agent, "_compress_context", return_value=([
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "summary"}], "You are helpful.")) as compress, \
         patch.object(agent, "_persist_session"), patch.object(agent, "_save_trajectory"), \
         patch.object(agent, "_cleanup_task_resources"):
        result = agent.run_conversation("continue", conversation_history=history)
    assert not result.get("failed"), result
    assert result["final_response"] == "Recovered"
    assert compress.call_count == 1
    assert compress.call_args.kwargs["trigger"] == "pre_api"
    assert len(sent) == 1


def test_aux_class_feasibility_does_not_probe_selector(route):
    agent = feasibility._make_agent(main_context=1050000)
    with patch("agent.auxiliary_client._resolve_task_provider_model",
               return_value=("auto", "class:deep", None, None, None)), \
         patch("agent.auxiliary_client.get_text_auxiliary_client",
               return_value=(SimpleNamespace(base_url="", api_key=""), "class:deep")), \
         patch("agent.model_metadata.get_model_context_length", return_value=256000) as metadata:
        agent._check_compression_model_feasibility()
    metadata.assert_not_called()
    assert agent.context_compressor.threshold_tokens == 272000
