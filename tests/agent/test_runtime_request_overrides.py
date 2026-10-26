"""Caller bodies survive same-route switches without leaking provider defaults."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.agent_init import _merge_custom_provider_extra_body
from agent.agent_runtime_helpers import _apply_switched_provider_request_overrides
from agent.agent_runtime_once import restore_runtime, snapshot_runtime
from agent.model_routing_agent import current_main_runtime
from tests.agent.test_primary_runtime_restore import (
    _make_agent, TestSwitchModelRequestOverridesSnapshot as _SwitchHarness,
)


def test_provider_defaults_are_replaced_but_caller_settings_survive():
    caller = {"reasoning": {"effort": "high"}}
    agent = SimpleNamespace(provider="custom", base_url="https://example.test/v1",
                            model="old", request_overrides={"extra_body": deepcopy(caller)})
    providers = [{"name": "local", "base_url": agent.base_url, "model": "old",
                  "extra_body": {"old_model_only": True, "reasoning": {"effort": "low"}}},
                 {"name": "local", "base_url": agent.base_url, "model": "new",
                  "extra_body": {"new_model_only": True, "reasoning": {"effort": "low"}}}]
    _merge_custom_provider_extra_body(agent, providers)
    agent._custom_providers = providers
    agent.model = "new"
    _apply_switched_provider_request_overrides(agent, "custom")
    assert agent.request_overrides == {"extra_body": {"new_model_only": True, **caller}}
    # The live request body cannot corrupt the caller's retained template.
    agent.request_overrides["extra_body"]["reasoning"]["effort"] = "low"
    _apply_switched_provider_request_overrides(agent, "custom")
    assert agent.request_overrides["extra_body"]["reasoning"]["effort"] == "high"


@pytest.mark.parametrize("provider,url", [("custom", "https://other.test/v1"),
                                          ("openai", "https://my-llm.example.com/v1")])
def test_caller_body_does_not_cross_provider_or_endpoint(provider, url):
    agent = _make_agent(request_overrides={"extra_body": {"reasoning": {"effort": "high"}}})
    _SwitchHarness()._switch(agent, new_provider=provider, base_url=url)
    assert "extra_body" not in agent.request_overrides
    assert "extra_body" not in agent._primary_runtime["request_overrides"]


def test_temporary_switch_and_failed_switch_preserve_caller_body():
    overrides = {"extra_body": {"reasoning": {"effort": "high"}}}
    agent = _make_agent(request_overrides=overrides)
    original = snapshot_runtime(agent)
    agent._one_turn_runtime = original
    _SwitchHarness()._switch(agent)
    assert "extra_body" not in agent.request_overrides
    restore_runtime(agent, original)
    assert agent.request_overrides == overrides
    with patch("agent.process_bootstrap.OpenAI", side_effect=RuntimeError("client build failed")), \
         pytest.raises(RuntimeError, match="client build failed"):
        agent.switch_model("other", "custom", api_key="synthetic", base_url=agent.base_url)
    assert agent.request_overrides == overrides
    _SwitchHarness()._switch(
        agent, new_provider="custom", base_url=agent.base_url)
    assert agent.request_overrides == overrides
    assert agent._primary_runtime["request_overrides"] == overrides


@pytest.mark.parametrize("metadata,expected", [(None, None), ({}, None),
                                              ({"requested_class": "deep"}, "deep")])
def test_main_runtime_class_metadata_is_optional(metadata, expected):
    agent = SimpleNamespace(model="exact-native", provider="anthropic", model_routing=metadata)
    runtime = current_main_runtime(agent)
    if expected is None:
        assert "requested_class" not in runtime
    else:
        assert runtime["requested_class"] == expected
    assert runtime["model"] == "exact-native"
