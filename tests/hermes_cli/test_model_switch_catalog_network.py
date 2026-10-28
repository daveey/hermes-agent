"""Dashboard cache-only switching keeps CLI catalog fetches available."""

import pytest

from agent import models_dev
from hermes_cli.model_switch import switch_model


@pytest.mark.parametrize("allow_network", [False, True])
@pytest.mark.parametrize("provider", ["openrouter", "racebox"])
def test_switch_catalog_network_policy(monkeypatch, allow_network, provider):
    monkeypatch.setattr(models_dev, "_models_dev_cache", {})
    monkeypatch.setattr(models_dev, "_models_dev_cache_time", 0)
    monkeypatch.setattr(models_dev, "_models_dev_retry_after", 0)
    monkeypatch.setattr(models_dev, "_load_disk_cache", dict)
    monkeypatch.setattr(models_dev, "_disk_cache_age_seconds", lambda: None)
    calls = []

    def fetch(**_kwargs):
        calls.append(True)
        raise OSError("offline")

    monkeypatch.setattr(models_dev, "_fetch_models_dev_from_network", fetch)
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **_kw: {
        "api_key": "test-key", "base_url": "http://endpoint/v1", "api_mode": "chat_completions"})
    monkeypatch.setattr("hermes_cli.models_validate.validate_requested_model", lambda *_a, **_kw: {
        "accepted": True, "persist": True, "recognized": True})
    kwargs: dict = {} if allow_network else {"allow_network": False}
    result = switch_model(
        "race-model", current_provider=provider, current_model="old-model", explicit_provider=provider,
        user_providers={"racebox": {"base_url": "http://endpoint/v1", "model": "race-model", "api_key": "k"}},
        **kwargs)
    assert result.success, result.error_message
    assert bool(calls) is allow_network
    # The per-operation policy must not leak into the next CLI request.
    if not allow_network:
        models_dev.fetch_models_dev()
        assert calls
