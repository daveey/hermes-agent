"""Persisted account identity, not pool cardinality, owns picker usage."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agent import account_usage, account_usage_cache as cache
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from hermes_cli.inventory import _apply_usage
from tests.hermes_cli import test_model_options_pool_usage as helpers


@pytest.fixture
def pool_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for store in (cache._snapshots, cache._started, cache._last_try, cache._inflight):
        store.clear()
    monkeypatch.setattr(cache, "refresh_account_usage_async", lambda providers: [])
    monkeypatch.setattr(cache, "refresh_account_usage_entries_async", lambda requests: [])
    return tmp_path


def row():
    return {"slug": "anthropic", "name": "Anthropic", "models": ["claude-opus"], "authenticated": True}


@pytest.mark.parametrize("persist_empty", [False, True])
@pytest.mark.parametrize("telemetry", ["absent", "stale", "fresh"])
def test_no_accounts_preserves_legacy_gauge(pool_home, persist_empty, telemetry):
    if persist_empty:
        helpers._write_pool(pool_home, "anthropic", [])
    if telemetry != "absent":
        snapshot = AccountUsageSnapshot(
            provider="anthropic", source="test",
            fetched_at=datetime.now(UTC) - timedelta(hours=1 if telemetry == "stale" else 0),
            windows=(AccountUsageWindow(label="Week", used_percent=12),))
        cache.remember_account_usage("anthropic", snapshot)
    provider = row()
    _apply_usage([provider])
    if telemetry == "fresh":
        assert provider["usage"] == {"windows": [{"label": "Week", "used_percent": 12.0,
                                                  "resets_at": None, "scope": "account"}]}
    else:
        assert "usage" not in provider


@pytest.mark.parametrize("size", [1, 2])
@pytest.mark.parametrize("telemetry", ["absent", "stale", "empty", "fresh", "dead"])
def test_persisted_accounts_remain_visible_without_live_telemetry(pool_home, size, telemetry):
    entries = [helpers._entry(f"c{i}", f"sk-ant-oat01-test-{i}", auth_type="oauth",
                              last_status="dead" if telemetry == "dead" else "ok") for i in range(size)]
    from agent.credential_pool import load_pool

    helpers._write_pool(pool_home, "anthropic", entries)
    load_pool("anthropic")  # Normalize the persisted fixture before the read-only assertion.
    before = (pool_home / "auth.json").read_bytes()
    snapshot = AccountUsageSnapshot(provider="anthropic", source="test", fetched_at=datetime.now(UTC),
                                    windows=(AccountUsageWindow(label="Week", used_percent=40),))
    if telemetry == "stale":
        snapshot = replace(snapshot, fetched_at=datetime.now(UTC) - timedelta(hours=1))
    if telemetry == "empty":
        snapshot = replace(snapshot, windows=())
    if telemetry != "absent":
        cache.remember_account_usage("anthropic", snapshot, identity_id=helpers._fp(entries[0]["access_token"]))
    # Ambient/provider-wide data must never impersonate a persisted account.
    cache.remember_account_usage("anthropic", replace(snapshot, windows=(AccountUsageWindow(label="Other", used_percent=99),)))
    provider = row()
    _apply_usage([provider])
    accounts = provider["usage"]["accounts"]
    assert len(accounts) == size
    assert "windows" not in provider["usage"]
    assert accounts[0]["id"] == helpers._fp(entries[0]["access_token"])
    assert accounts[0]["label"] == "c0"
    expected = "unavailable" if telemetry == "dead" else "ready" if telemetry == "fresh" else "unknown"
    assert accounts[0]["state"] == expected
    assert accounts[0]["windows"] == ([{"label": "Week", "used_percent": 40.0, "resets_at": None, "scope": "account"}]
                                       if telemetry == "fresh" else [])
    assert (pool_home / "auth.json").read_bytes() == before


@pytest.mark.parametrize("fails", [False, True])
def test_single_pool_refresh_uses_exact_row_never_ambient(pool_home, monkeypatch, fails):
    token = "sk-ant-oat01-pool-test"
    from agent.credential_pool import load_pool

    helpers._write_pool(pool_home, "anthropic", [helpers._entry("only", token, auth_type="oauth")])
    load_pool("anthropic")
    before = (pool_home / "auth.json").read_bytes()
    calls = []
    def fail_ambient():
        pytest.fail("persisted pool refresh consulted ambient singleton")
    def get_json(url, headers, **kwargs):
        calls.append(headers["Authorization"])
        if fails:
            raise RuntimeError("usage endpoint unavailable")
        return {"five_hour": None, "seven_day": {"utilization": 100, "resets_at": "2099-01-01T00:00:00Z"}}
    monkeypatch.setattr(account_usage, "resolve_anthropic_token", fail_ambient)
    monkeypatch.setattr(account_usage, "_get_json", get_json)
    # Exercise the actual refresh worker + fetcher, synchronously for deterministic readback.
    monkeypatch.setattr(cache, "refresh_account_usage_async", lambda providers: [cache._refresh_legacy(p, cache._key(p)) for p in providers])
    monkeypatch.setattr(cache, "refresh_account_usage_entries_async", cache._refresh_entries)
    provider = row()
    _apply_usage([provider])
    _apply_usage([provider])
    assert calls == [f"Bearer {token}", f"Bearer {token}"]
    account = provider["usage"]["accounts"][0]
    assert account["state"] == ("unknown" if fails else "limited")
    assert account["windows"] == ([] if fails else [{"label": "Current week", "used_percent": 100.0,
                                                       "resets_at": "2099-01-01T00:00:00+00:00", "scope": "account"}])
    assert (pool_home / "auth.json").read_bytes() == before


def test_single_pool_model_cooldown_is_not_account_readiness(pool_home):
    now = datetime.now(UTC).timestamp()
    helpers._write_pool(pool_home, "anthropic", [helpers._entry("only", "sk-ant-oat01-test", auth_type="oauth",
        model_cooldowns={"claude-opus": now + 1800})])
    provider = row()
    _apply_usage([provider])
    account = provider["usage"]["accounts"][0]
    assert account["state"] == "unknown"
    assert account["model_cooldowns"] == [{"model": "claude-opus", "until": datetime.fromtimestamp(now + 1800, UTC).isoformat()}]
