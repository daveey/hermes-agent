"""Memo must follow asynchronous pricing publication and time-based expiry immediately."""
import pytest

from hermes_cli import inventory, models, models_pricing as pricing


@pytest.fixture
def picker(monkeypatch):
    monkeypatch.setattr(pricing, "_pricing_cache", {})
    monkeypatch.setattr(pricing, "_pricing_cache_retry_after", {})
    monkeypatch.setattr(pricing, "_pricing_provider_cache_keys", {("fixture", "nous"): "first"})
    clock = {"t": 100.0}
    tier = {"value": False}
    monkeypatch.setattr(pricing.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(inventory, "_memo_clock", lambda: 100.0)  # hold memo TTL while pricing expires
    monkeypatch.setattr(models, "get_cached_nous_free_tier", lambda: tier["value"])
    monkeypatch.setattr(inventory, "_attach_nous_plan_usage", lambda *a, **k: None)
    monkeypatch.setattr(pricing, "get_pricing_for_provider", lambda *a, **k: pricing._cached_catalog(
        pricing._pricing_provider_cache_keys[("fixture", "nous")]))

    def build(ctx, **kwargs):
        payload = {"providers": [{"slug": "nous", "models": ["fixture-model"]}],
                   "model": ctx.current_model, "provider": ctx.current_provider}
        inventory._apply_pricing(payload["providers"], cached_only=True)
        return payload

    monkeypatch.setattr(inventory, "build_models_payload", build)
    monkeypatch.setattr(inventory, "_apply_limits", lambda rows: None)
    monkeypatch.setattr(inventory, "_apply_usage", lambda rows: None)
    monkeypatch.setattr(inventory, "_prewarm_pricing_async", lambda *a, **k: None)
    monkeypatch.setattr(inventory, "_MEMO_ENABLED", True)
    inventory.reset_model_options_memo()
    ctx = inventory.ConfigContext("nous", "fixture-model", "", {}, [])
    yield lambda: inventory.build_model_options_payload(ctx), clock, tier
    inventory.reset_model_options_memo()


CATALOG = {"fixture-model": {"prompt": "0.000001", "completion": "0.000002"}}


@pytest.mark.parametrize("change", ["publish", "replace", "expire", "mapping", "entitlement"])
def test_pricing_changes_invalidate_payload_memo(picker, monkeypatch, change):
    call, clock, tier = picker
    if change != "publish":
        pricing._cache_catalog("first", CATALOG, ttl_seconds=1.0 if change == "expire" else None)
    before = call()
    assert call() == before
    changes = {
        "publish": lambda: pricing._cache_catalog("first", CATALOG),
        "replace": lambda: pricing._cache_catalog("first", {
            "fixture-model": {"prompt": "0.000003", "completion": "0.000004"}}),
        "expire": lambda: clock.update(t=clock["t"] + 2),
        # No catalog mutation: only the provider's selected endpoint cache changes.
        "mapping": lambda: pricing._pricing_provider_cache_keys.update({("fixture", "nous"): "absent"}),
        "entitlement": lambda: tier.update(value=None),
    }
    changes[change]()
    got = call()
    monkeypatch.setattr(inventory, "_MEMO_ENABLED", False)
    fresh = call()
    assert fresh != before  # positive control: this input changes real pricing decoration
    assert got == fresh


@pytest.mark.parametrize("evict_first", [False, True])
def test_expired_prices_never_resurrect_after_reader_eviction(picker, monkeypatch, evict_first):
    call, clock, _tier = picker
    pricing._cache_catalog("first", CATALOG, ttl_seconds=1.0)
    before = call()
    assert call() == before
    clock["t"] += 2
    if evict_first:
        assert pricing._cached_catalog("first") is None
    # Cover both another consumer evicting first and the picker doing its own eviction.
    # Keep the memo clock frozen so TTL expiry cannot hide an obsolete stamp's reuse.
    for _ in range(3):
        got = call()
        with monkeypatch.context() as patch:
            patch.setattr(inventory, "_MEMO_ENABLED", False)
            fresh = call()
        assert fresh != before
        assert got == fresh
