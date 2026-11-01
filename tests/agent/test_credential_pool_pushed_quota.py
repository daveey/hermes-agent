"""Token replacement repairs authentication, not an account's quota window."""
import json
import time

import pytest

import agent.credential_pool as cp
from hermes_cli import auth


@pytest.mark.parametrize("code,status,delta,keep", [
    (429, "exhausted", 3600, True),
    (401, "dead", 3600, False),
    (429, "exhausted", -60, False),
])
def test_pushed_token_quota_policy(tmp_path, monkeypatch, code, status, delta, keep):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = time.time()
    row = dict(id="aaaaaa", label="example", auth_type="oauth", priority=0,
               source="manual:hub_minted", access_token="old-dummy",
               expires_at_ms=int((now + 7200) * 1000), last_status=status,
               last_status_at=now - 10, last_error_code=code,
               last_error_reason="usage_limit_reached" if code == 429 else "invalid_token",
               last_error_reset_at=now + delta,
               model_cooldowns={"example-model": now + 7200})
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": [row]}}))
    entry = cp.PooledCredential.from_dict("openai-codex", row)
    pool = cp.CredentialPool("openai-codex", [entry])
    row["access_token"] = "new-dummy"
    path.write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": [row]}}))
    adopted = pool._adopt_pushed_norefresh_row(entry)
    persisted = auth.read_credential_pool("openai-codex")[0]
    assert adopted.access_token == persisted["access_token"] == "new-dummy"
    assert adopted.model_cooldowns == persisted["model_cooldowns"] == row["model_cooldowns"]
    assert adopted.last_status == (status if keep else "ok")
    assert adopted.last_error_reset_at == (row["last_error_reset_at"] if keep else None)
    assert persisted.get("last_error_code") == (code if keep else None)
    if keep:
        assert not persisted.get("status_cleared_at")
        assert pool._available_entries()[0] == []


@pytest.mark.parametrize("path", ["refresh", "select"])
def test_refresh_does_not_lease_pushed_quota_row(tmp_path, monkeypatch, path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    now = time.time()
    row = dict(id="aaaaaa", label="example", auth_type="oauth", priority=0,
               source="manual:hub_minted", access_token="old-dummy",
               expires_at_ms=int((now - 60) * 1000), last_status="ok")
    entry = cp.PooledCredential.from_dict("anthropic", row)
    pool = cp.CredentialPool("anthropic", [entry])
    row.update(access_token="new-dummy", expires_at_ms=int((now + 7200) * 1000),
               last_status="exhausted", last_status_at=now, last_error_code=429,
               last_error_reason="usage_limit_reached", last_error_reset_at=now + 3600)
    (tmp_path / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"anthropic": [row]}}))
    result = pool._refresh_entry(entry, force=True) if path == "refresh" else pool.select()
    assert result is None
    assert pool.entries()[0].last_status == "exhausted"
    assert auth.read_credential_pool("anthropic")[0]["last_error_reset_at"] == now + 3600
