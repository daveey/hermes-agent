"""Window-scoped evidence at the real HTTP probe and singleton resolver."""
import json
import logging

import httpx
import pytest

from hermes_cli import auth_codex
from hermes_cli.auth_codex_quota_window import quota_restored
from tests.agent.test_codex_rotation_window import _pool, _rotate


@pytest.mark.parametrize("delta, expected", [(0, True), (60, True), (-60, True), (61, False)])
def test_window_reset_tolerance(delta, expected):
    assert quota_restored({"rate_limit": {"primary_window": {
        "used_percent": 0, "reset_at": 1000 + delta}}}, 1000) is expected


@pytest.mark.parametrize("used", [True, float("nan"), float("inf"), -1, 100, "0", None])
def test_invalid_or_exhausted_usage_is_not_evidence(used):
    assert quota_restored({"rate_limit": {"primary_window": {
        "used_percent": used, "reset_at": 1000}}}, 1000) is False


@pytest.mark.parametrize("denial", [{"allowed": False}, {"limit_reached": True}])
def test_account_denial_overrides_open_window(denial):
    assert quota_restored({"rate_limit": {**denial, "primary_window": {
        "used_percent": 0, "reset_at": 1000}}}, 1000) is False


def test_model_window_wall_overrides_account_evidence():
    assert quota_restored({"rate_limit": {"primary_window": {"used_percent": 0, "reset_at": 1000}},
                          "additional_rate_limits": [{"rate_limit": {"primary_window": {"used_percent": 100}}}]},
                         1000) is False


def test_cache_cannot_reuse_older_failure_and_expires(tmp_path, monkeypatch):
    _, rows, reset = _pool(tmp_path, monkeypatch)
    calls = []
    payload = {"rate_limit": {"primary_window": {"used_percent": 0, "reset_at": reset}}}
    def respond(request):
        calls.append(request.url)
        return httpx.Response(200, json=payload)
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(auth_codex, "_codex_http_client", lambda **kwargs: httpx.Client(transport=transport))
    clock = [1000.0]
    monkeypatch.setattr(auth_codex.time, "monotonic", lambda: clock[0])
    probe = auth_codex._probe_codex_quota_restored
    token = rows[0]["access_token"]
    assert probe(token, reset_at=reset, failure_at=1) is True
    assert probe(token, reset_at=reset, failure_at=1) is True
    assert probe(token, reset_at=reset, failure_at=2) is None
    assert probe(token, reset_at=reset + 100, failure_at=1) is None
    assert len(calls) == 1
    clock[0] += 300
    assert probe(token, reset_at=reset, failure_at=2) is True
    assert len(calls) == 2


def test_singleton_short_window_cannot_clear_weekly_wall(tmp_path, monkeypatch):
    pool, _, reset = _pool(tmp_path, monkeypatch, other_walled=True)
    assert _rotate(pool, reset) is None
    auth_codex._codex_quota_probe_cache.clear()
    with pytest.raises(auth_codex.AuthError, match="quota exhausted"): 
        auth_codex.resolve_codex_runtime_credentials()
    saved = json.loads((tmp_path / "auth.json").read_text())["credential_pool"]["openai-codex"]
    assert all(row["last_status"] == "exhausted" for row in saved)


def test_singleton_positive_probe_only_clears_proven_row(tmp_path, monkeypatch, caplog):
    pool, rows, reset = _pool(tmp_path, monkeypatch, other_walled=True)
    assert _rotate(pool, reset) is None
    auth_codex._codex_quota_probe_cache.clear()
    payload = {"rate_limit": {"primary_window": {"used_percent": 0, "reset_at": reset}}}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    monkeypatch.setattr(auth_codex, "_codex_http_client", lambda **kwargs: httpx.Client(transport=transport))
    with caplog.at_level(logging.INFO):
        result = auth_codex.resolve_codex_runtime_credentials()
    assert result["api_key"] == rows[0]["access_token"]
    saved = json.loads((tmp_path / "auth.json").read_text())["credential_pool"]["openai-codex"]
    assert saved[0]["last_status"] is None
    assert all(row["last_status"] == "exhausted" for row in saved[1:])
    assert "mechanism=singleton_quota_probe" in caplog.text
    assert "old_reset=" in caplog.text


@pytest.mark.parametrize("field,value", [("id", "other"), ("last_status_at", "newer"),
                                        ("reset_at", 1), ("access_token", "different")])
def test_singleton_clear_rejects_changed_generation(tmp_path, monkeypatch, field, value):
    from hermes_cli.auth_codex_quota_window import clear_proven_cooldown
    pool, _, reset = _pool(tmp_path, monkeypatch, other_walled=True)
    assert _rotate(pool, reset) is None
    proven = auth_codex._codex_pool_rate_limit_status()
    proven[field] = value
    before = (tmp_path / "auth.json").read_bytes()
    assert clear_proven_cooldown(proven) is False
    assert (tmp_path / "auth.json").read_bytes() == before
