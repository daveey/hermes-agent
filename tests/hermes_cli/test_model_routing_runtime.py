"""Real config/pool imports under a disposable home; no provider calls."""
import json

import pytest

from agent.model_routing import ModelRoutingError
from hermes_cli.runtime_provider import resolve_runtime_provider, resolve_runtime_with_fallback
from tests.agent.test_model_routing import policy


@pytest.fixture
def routing_home(tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = policy()
    cfg["model_routing"]["routes"]["deep"] = cfg["model_routing"]["routes"]["deep"][:1]
    cfg["fallback_model"] = [{"provider": "openrouter", "model": "paid-wrong-class"}]
    atomic_config_write(tmp_path / "config.yaml", cfg)
    entries = [
        {"id": "paid", "label": "paid", "priority": 0, "auth_type": "api_key",
         "source": "manual", "access_token": "sk-ant-api03-synthetic-paid"},
        {"id": "subscription", "label": "subscription", "priority": 1, "auth_type": "oauth",
         "source": "manual", "access_token": "sk-ant-oat01-synthetic-oauth"},
    ]
    (tmp_path / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": entries}}), encoding="utf-8")
    return cfg


def test_runtime_uses_native_subscription_not_first_paid_account(routing_home):
    rt = resolve_runtime_provider(requested="auto", target_model="class:deep", routing_session_id="session")
    assert rt["model"] == "anthropic-deep"
    assert rt["api_mode"] == "anthropic_messages"
    assert rt["api_key"] == "sk-ant-oat01-synthetic-oauth"
    assert rt["model_routing"]["billing_group"] == "subscription"
    assert rt["credential_pool"].current().id == "subscription"


@pytest.mark.parametrize("status", [401, 429, 408])
def test_pool_rotation_cannot_spend_paid_key(routing_home, status):
    rt = resolve_runtime_provider(requested="auto", target_model="class:deep", routing_session_id="session")
    pool = rt["credential_pool"]
    next_entry = pool.mark_exhausted_and_rotate(status_code=status, credential_id="subscription", model=rt["model"])
    assert next_entry is None
    assert any(e.id == "paid" for e in pool.entries())  # fence does not delete the user's key
    with pytest.raises(ModelRoutingError):
        resolve_runtime_with_fallback(routing_home, requested="auto", target_model="class:deep",
                                      routing_session_id="another")


def test_explicit_endpoint_cannot_override_subscription(routing_home):
    with pytest.raises(ModelRoutingError, match="endpoint"):
        resolve_runtime_provider(requested="auto", target_model="class:deep", routing_session_id="session",
                                 explicit_base_url="https://example.test")


def test_no_class_marker_resolves_without_identity(routing_home):
    with pytest.raises(ModelRoutingError, match="identity"):
        resolve_runtime_provider(requested="auto", target_model="class:deep")
