"""A scoped catalog policy must be cache-only, nested and exception safe."""

import pytest

from agent import models_dev


@pytest.mark.parametrize("source", ["memory", "disk", "cold"])
def test_cache_only_scope_serves_available_data_and_restores_on_error(monkeypatch, source):
    registry = {"example": {"name": "Example", "models": {}}}
    monkeypatch.setattr(models_dev, "_models_dev_cache", registry if source == "memory" else {})
    monkeypatch.setattr(models_dev, "_models_dev_cache_time", 0)
    monkeypatch.setattr(models_dev, "_models_dev_retry_after", 0)
    monkeypatch.setattr(models_dev, "_load_disk_cache", lambda: registry if source == "disk" else {})
    monkeypatch.setattr(models_dev, "_disk_cache_age_seconds", lambda: None)
    calls = []

    def fetch(**_kwargs):
        calls.append(True)
        raise OSError("offline")

    monkeypatch.setattr(models_dev, "_fetch_models_dev_from_network", fetch)
    with pytest.raises(ValueError, match="test unwind"):
        with models_dev.models_dev_network_scope(allow_network=False):
            with models_dev.models_dev_network_scope(allow_network=True):
                assert models_dev.fetch_models_dev(force_refresh=True) == ({} if source == "cold" else registry)
                assert not calls
                raise ValueError("test unwind")
    models_dev.fetch_models_dev(force_refresh=True)
    assert calls
