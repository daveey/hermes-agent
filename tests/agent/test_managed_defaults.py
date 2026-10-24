"""Omitted runtime targets honor effective managed defaults, not guessed classes."""
import pytest

from hermes_cli.config import atomic_config_write
from hermes_cli.runtime_provider import resolve_runtime_provider
from tests.agent import test_managed_subscription as fixtures

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


@pytest.mark.parametrize("default", ["class:deep", "anthropic-deep"])
def test_missing_target_uses_managed_default(managed, tmp_path, default):
    managed.setdefault("model", {}).update(default=default, provider="auto")
    atomic_config_write(tmp_path / "config.yaml", managed)
    runtime = resolve_runtime_provider(routing_session_id="default-job")
    assert runtime["model"] == "anthropic-deep"
    assert runtime["provider"] == "anthropic"
    assert runtime["model_routing"].get("exact_pin", False) == (default == "anthropic-deep")
