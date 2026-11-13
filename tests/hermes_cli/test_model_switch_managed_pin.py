"""``/model`` refuses a concrete pin the managed routing registry would refuse at every turn. [daveey]

A Desktop chat switched to a model this host's ``model_routing.routes`` lacks: the switch saved it, then
every turn died before the model with "Managed subscription policy rejects unregistered or ambiguous
concrete pin", naming neither the model nor what the host allows. The switch now runs the same
``pin_policy`` check before anything is saved, and the error names the model and the allowed pins.
"""
import pytest

from agent.model_routing_managed import ManagedPinRejectedError, allowed_pins, managed_config, pin_policy
from hermes_cli import model_switch
from tests.agent import test_managed_subscription as fixtures

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


@pytest.fixture
def offline_validation(monkeypatch):
    """The catalog check accepts anything: these tests are about the routing registry, not the catalog."""
    import hermes_cli.models_validate as validate
    monkeypatch.setattr(validate, "validate_requested_model",
                        lambda *a, **k: {"accepted": True, "persist": True, "recognized": True, "message": ""})


def test_rejection_names_the_model_provider_and_every_allowed_pin(managed):
    with pytest.raises(ManagedPinRejectedError) as info:
        pin_policy("claude-fable-5-2", "anthropic", managed_config())
    text = str(info.value)
    assert "claude-fable-5-2" in text and "anthropic" in text
    assert all(model in text for model in info.value.allowed)
    assert "/model" in text and "model_routing.routes" in text
    # Class and attributes the Kanban/cron readers rely on are unchanged.
    assert info.value.model == "claude-fable-5-2" and info.value.provider == "anthropic"
    assert info.value.allowed and info.value.registry_empty is False


@pytest.mark.parametrize("explicit", ["", "anthropic"])
def test_switch_to_unregistered_model_is_refused_naming_allowed_pins(managed, offline_validation, explicit):
    result = model_switch.switch_model(
        "claude-fable-5-2", current_provider="anthropic", current_model="anthropic-deep",
        explicit_provider=explicit, allow_network=False)
    assert result.success is False
    assert "claude-fable-5-2" in result.error_message
    allowed = allowed_pins(managed_config())
    assert len(allowed) > 1 and all(model in result.error_message for model in allowed)
    assert "not connected" not in result.error_message  # a routing verdict, not a missing credential


def test_switch_to_registered_model_still_switches(managed, offline_validation):
    result = model_switch.switch_model(
        "anthropic-standard", current_provider="anthropic", current_model="anthropic-deep", allow_network=False)
    assert result.success is True, result.error_message
    assert result.new_model == "anthropic-standard"


def test_policy_off_accepts_any_model(routing_home, offline_validation):
    result = model_switch.switch_model(
        "claude-fable-5-2", current_provider="anthropic", current_model="anthropic-deep", allow_network=False)
    assert result.success is True, result.error_message


def test_class_selector_passes_the_pin_verdict(managed):
    from hermes_cli.model_switch_managed import refuse_unregistered_pin
    st = model_switch._Switch(
        raw_input="class:deep", current_provider="anthropic", current_model="anthropic-deep", current_base_url="",
        current_api_key="", is_global=False, explicit_provider="", user_providers=None, custom_providers=None,
        new_model="class:deep", target_provider="auto")
    assert refuse_unregistered_pin(st) is None
