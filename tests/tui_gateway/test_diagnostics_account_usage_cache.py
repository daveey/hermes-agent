"""diagnostics.account_usage_cache RPC: read-only view of the usage cache's refresh claims.

Pinned: the result validates against its declared contract, reports an inflight claim with its
age for the scoped home, and leaves every claim untouched.
"""

from __future__ import annotations

import time

import pytest

import agent.account_usage_cache as cache
from tui_gateway import server
from tui_gateway.contracts.config_free_tier_control import DiagnosticsAccountUsageCacheResult


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for store in (cache._snapshots, cache._started, cache._last_try, cache._inflight):
        store.clear()
    yield
    for store in (cache._snapshots, cache._started, cache._last_try, cache._inflight):
        store.clear()


def test_rpc_reports_inflight_claim_and_is_read_only():
    fn = server._methods.get("diagnostics.account_usage_cache")
    assert fn is not None, "diagnostics.account_usage_cache not registered"
    key = cache._key("openai-codex", "codex:acct:user")
    cache._claim_refresh(key, time.monotonic() - 3600)  # a claim wedged for an hour

    resp = fn("r1", {})
    result = resp["result"]
    DiagnosticsAccountUsageCacheResult.model_validate(result)
    assert result["inflight_count"] == 1
    (slot,) = result["slots"]
    assert slot["provider"] == "openai-codex" and slot["identity_id"] == "codex:acct:user"
    assert slot["inflight"] is True and slot["inflight_age_s"] >= 3599
    assert slot["stale"] is True and slot["snapshot_age_s"] is None
    assert key in cache._inflight, "a diagnostic must never release or expire a claim"
