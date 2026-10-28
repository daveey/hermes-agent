"""A cold models.dev fetch must not monopolize dashboard config writes."""

import threading

import pytest

from tests.hermes_cli.test_config_rmw_lock import client as rmw_client, _on_disk


@pytest.mark.parametrize("operation", ["activate", "make_default"])
def test_blocked_catalog_fetch_does_not_block_config_autosave(request, monkeypatch, operation):
    client = request.getfixturevalue("rmw_client")
    from agent import models_dev
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    cfg["providers"] = {"racebox": {"base_url": "http://racebox:8000/v1", "model": "race-model", "api_key": "k"}}
    save_config(cfg)
    monkeypatch.setattr(models_dev, "_models_dev_cache", {})
    monkeypatch.setattr(models_dev, "_models_dev_cache_time", 0)
    monkeypatch.setattr(models_dev, "_models_dev_retry_after", 0)
    monkeypatch.setattr(models_dev, "_load_disk_cache", dict)
    monkeypatch.setattr(models_dev, "_disk_cache_age_seconds", lambda: None)
    fetching, release, saved = threading.Event(), threading.Event(), threading.Event()
    results, errors = {}, []

    def blocked_fetch(**_kwargs):
        fetching.set()
        release.wait()
        raise OSError("catalog unavailable")

    monkeypatch.setattr(models_dev, "_fetch_models_dev_from_network", blocked_fetch)

    def send_request(name, callback):
        try:
            results[name] = callback()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if name == "autosave":
                saved.set()

    # Hold a real foreground catalog fetch open. The dashboard's model writer
    # must not join it while holding the config RMW lock.
    catalog = threading.Thread(target=lambda: models_dev.fetch_models_dev())
    def select_model():
        if operation == "activate":
            return client.post("/api/providers/custom-endpoints/racebox/activate")
        return client.post("/api/providers/custom-endpoints", json={
            "id": "racebox", "name": "racebox", "base_url": "http://racebox:8000/v1",
            "model": "race-model", "make_default": True, "discover_models": False})

    activate = threading.Thread(target=send_request, args=("activate", select_model))
    autosave = threading.Thread(target=send_request, args=("autosave", lambda: client.put(
        "/api/config", json={"config": {"display": {"personality": "canary"}}})))
    reached_switch = threading.Event()
    from hermes_cli import model_switch
    real_label = model_switch._switch_provider_label

    def label(st):
        # This point is reached under the config lock, before the old blocking lookup.
        reached_switch.set()
        return real_label(st)

    monkeypatch.setattr(model_switch, "_switch_provider_label", label)
    catalog.start()
    try:
        assert fetching.wait(10), "catalog fetch did not start"
        activate.start()
        assert reached_switch.wait(10), "activation did not reach model switch"
        autosave.start()
        assert saved.wait(10), "autosave waited for the blocked catalog fetch"
        assert not release.is_set()
    finally:
        release.set()
        for thread in (catalog, activate, autosave):
            if thread.ident is not None:
                thread.join(30)
                assert not thread.is_alive()
    assert not errors
    assert results["activate"].status_code == 200, results["activate"].text
    assert results["autosave"].status_code == 200, results["autosave"].text
    assert _on_disk()["model"]["provider"] == "racebox"
    assert _on_disk()["display"]["personality"] == "canary"
