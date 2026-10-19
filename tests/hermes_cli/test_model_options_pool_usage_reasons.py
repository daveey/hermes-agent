"""Why an account is limited: sanitized cause fields on ``usage.accounts`` rows.

Real auth stores on disk, real ``hermes_cli/inventory.py::_apply_usage``. ``state`` keeps its
meaning (``limited`` still blocks selection); the additive fields say whether the account is out
of quota (``limit_kind="quota"``, red) or on a temporary cooldown (``"cooldown"``, amber), when the
cooldown ends, and which models alone are cooling down. Raw provider messages never ride the wire.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from agent import account_usage
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from agent.account_usage_cache import _snapshots, remember_account_usage
from hermes_cli.inventory import _apply_usage

SECRET_MESSAGE = "Error 429 for key sk-or-v1-SECRETSECRET: Rate limit exceeded (request id req_abc)"


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch):
    _snapshots.clear()
    monkeypatch.setitem(account_usage._USAGE_FETCHERS, "openrouter", lambda base_url, api_key: None)
    yield
    import threading

    for worker in [t for t in threading.enumerate() if "hermes-account-usage-refresh" in t.name]:
        worker.join(10)
    _snapshots.clear()


def _fp(token: str) -> str:
    import hashlib

    return f"fp:{hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]}"


def _entry(cred_id: str, token: str, **over) -> dict:
    entry = {"id": cred_id, "label": cred_id, "auth_type": "api_key", "priority": 0,
             "source": "manual", "access_token": token}
    entry.update(over)
    return entry


def _snapshot(used: float, *, scope: str = "account") -> AccountUsageSnapshot:
    now = datetime.now(UTC)
    return AccountUsageSnapshot(
        provider="openrouter", source="test", fetched_at=now,
        windows=(AccountUsageWindow(label="Session", used_percent=used,
                                    reset_at=now + timedelta(hours=2), scope=scope),))


def _accounts(monkeypatch, tmp_path, slug: str, entries: list[dict], usage: dict | None = None) -> dict:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {slug: entries}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    for token, snap in (usage or {}).items():
        remember_account_usage(slug, snap, identity_id=_fp(token))
    before = (home / "auth.json").read_text()
    row = {"slug": slug, "name": slug, "models": ["m"], "authenticated": True}
    _apply_usage([row])
    assert (home / "auth.json").read_text() == before, "read-only inventory must never rewrite auth.json"
    return {a["label"]: a for a in row["usage"]["accounts"]}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def test_live_rate_limit_cooldown_under_100_percent_is_amber_cooldown(monkeypatch, tmp_path):
    """Quota has room (40%) but the pool benched the credential for a generic 429: still
    ``limited`` (selection stays blocked), classified as a temporary cooldown with its expiry."""
    status_at = time.time() - 60
    accounts = _accounts(monkeypatch, tmp_path, "openrouter", [
        _entry("benched", "tok-1", last_status="exhausted", last_status_at=status_at,
               last_error_code=429, last_error_message=SECRET_MESSAGE, failure_reason="rate_limit"),
        _entry("healthy", "tok-2"),
    ], usage={"tok-1": _snapshot(40.0), "tok-2": _snapshot(10.0)})

    benched = accounts["benched"]
    assert benched["state"] == "limited"
    assert benched["limit_kind"] == "cooldown"
    assert benched["reason"] == "rate_limit"
    assert benched["cooldown_until"] is not None
    assert datetime.fromisoformat(benched["cooldown_until"]).timestamp() > time.time()
    assert benched["resets_at"] == benched["cooldown_until"]
    assert benched["windows"][0]["used_percent"] == 40.0
    assert accounts["healthy"]["state"] == "ready"
    assert accounts["healthy"]["limit_kind"] is None and accounts["healthy"]["reason"] is None
    wire = json.dumps(accounts)
    assert "SECRET" not in wire and "sk-or" not in wire and "req_abc" not in wire


def test_expired_cooldown_is_ready_with_no_cause(monkeypatch, tmp_path):
    accounts = _accounts(monkeypatch, tmp_path, "openrouter", [
        _entry("recovered", "tok-1", last_status="exhausted", last_status_at=time.time() - 3 * 3600,
               last_error_code=429),
        _entry("other", "tok-2"),
    ], usage={"tok-1": _snapshot(20.0), "tok-2": _snapshot(10.0)})

    row = accounts["recovered"]
    assert row["state"] == "ready"
    assert row["limit_kind"] is None and row["reason"] is None and row["cooldown_until"] is None


def test_true_exhausted_window_is_red_quota(monkeypatch, tmp_path):
    accounts = _accounts(monkeypatch, tmp_path, "openrouter", [
        _entry("spent", "tok-1"), _entry("other", "tok-2"),
    ], usage={"tok-1": _snapshot(100.0), "tok-2": _snapshot(10.0)})

    row = accounts["spent"]
    assert row["state"] == "limited"
    assert row["limit_kind"] == "quota"
    assert row["reason"] == "quota_window"
    assert row["cooldown_until"] is None


def test_exhausted_window_outranks_a_live_cooldown(monkeypatch, tmp_path):
    accounts = _accounts(monkeypatch, tmp_path, "openrouter", [
        _entry("both", "tok-1", last_status="exhausted", last_status_at=time.time() - 60, last_error_code=429),
        _entry("other", "tok-2"),
    ], usage={"tok-1": _snapshot(100.0), "tok-2": _snapshot(10.0)})

    row = accounts["both"]
    assert row["limit_kind"] == "quota" and row["reason"] == "quota_window"
    assert row["cooldown_until"] is not None  # the bench is still shown with its expiry


def test_billing_cooldown_is_quota(monkeypatch, tmp_path):
    accounts = _accounts(monkeypatch, tmp_path, "openrouter", [
        _entry("broke", "tok-1", last_status="exhausted", last_status_at=time.time() - 60,
               last_error_code=402, failure_reason="billing"),
        _entry("other", "tok-2"),
    ])
    assert accounts["broke"]["state"] == "limited"
    assert (accounts["broke"]["limit_kind"], accounts["broke"]["reason"]) == ("quota", "billing")


def test_model_only_cooldown_does_not_limit_the_account(monkeypatch, tmp_path):
    """A per-model 429 cooldown leaves the credential usable for other models: the account is not
    limited, and the cooled-down model is listed with its expiry. Entitlement benches (a year) and
    expired cooldowns are not listed."""
    now = time.time()
    accounts = _accounts(monkeypatch, tmp_path, "openrouter", [
        _entry("partial", "tok-1", model_cooldowns={
            "model-a": now + 1800, "model-old": now - 60, "model-plan": now + 365 * 86400}),
        _entry("other", "tok-2"),
    ], usage={"tok-1": _snapshot(30.0), "tok-2": _snapshot(10.0)})

    row = accounts["partial"]
    assert row["state"] == "ready"
    assert row["limit_kind"] is None and row["cooldown_until"] is None
    assert row["model_cooldowns"] == [{"model": "model-a", "until": _iso(now + 1800)}]
    assert accounts["other"]["model_cooldowns"] == []


def test_codex_explicit_usage_limit_reset_is_quota_until_reset(monkeypatch, tmp_path):
    """Codex ``usage_limit_reached`` with the provider's explicit reset (days out) is a real
    exhaustion: red quota until that reset, even though the pool classified it as a 429."""
    reset_at = float(int(time.time() + 4 * 86400))
    accounts = _accounts(monkeypatch, tmp_path, "openai-codex", [
        _entry("device_code", "tok-1", auth_type="oauth", last_status="exhausted",
               last_status_at=time.time() - 2 * 86400, last_error_code=429,
               last_error_reason="usage_limit_reached", last_error_reset_at=reset_at,
               failure_reason="rate_limit"),
        _entry("other", "tok-2", auth_type="oauth"),
    ])

    row = accounts["device_code"]
    assert row["state"] == "limited"
    assert (row["limit_kind"], row["reason"]) == ("quota", "usage_limit")
    assert row["cooldown_until"] == _iso(reset_at)
    assert row["resets_at"] == _iso(reset_at)


def test_dead_row_reason_is_auth(monkeypatch, tmp_path):
    accounts = _accounts(monkeypatch, tmp_path, "openrouter", [
        _entry("dead", "tok-1", last_status="dead", last_status_at=time.time() - 60),
        _entry("other", "tok-2"),
    ])
    assert accounts["dead"]["state"] == "unavailable"
    assert accounts["dead"]["reason"] == "auth"
    assert accounts["dead"]["limit_kind"] is None
