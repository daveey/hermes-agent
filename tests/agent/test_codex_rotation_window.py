"""Real pool/probe regression: a short-window response cannot undo a weekly 429."""
import base64
import json
import logging
import time

import httpx
import pytest

from agent.credential_pool import load_pool
from agent.model_routing_runtime import verify_class_runtime
from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
from hermes_cli import auth, auth_codex


def _token(label):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 3600, "sub": label}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{payload}.sig"


def _pool(tmp_path, monkeypatch, *, other_walled=False, payload=None):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    auth._codex_quota_probe_cache.clear()
    reset = time.time() + 6 * 86400
    rows = []
    for index, label in enumerate("ABC"):
        row = dict(id=label, label=label, priority=index, source="manual", auth_type="oauth",
                   access_token=_token(label), refresh_token=f"refresh-{label}")
        if label == "C" or (label == "B" and other_walled):
            row.update(last_status="exhausted", last_status_at=time.time(), last_error_code=429,
                       last_error_reason="usage_limit_reached", last_error_reset_at=reset)
        rows.append(row)
    (tmp_path / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"openai-codex": rows}}))
    response = payload or {"rate_limit": {"primary_window": {"used_percent": 0, "reset_at": time.time() + 18000}}}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    monkeypatch.setattr(auth_codex, "_codex_http_client", lambda **kwargs: httpx.Client(transport=transport))
    pool = load_pool("openai-codex")
    return pool, rows, reset


def _verify(pool, token):
    verify_class_runtime(dict(provider="openai-codex", api_key=token, credential_pool=pool,
                              base_url="https://chatgpt.com/backend-api/codex", api_mode="codex_responses"),
                         dict(selected_provider="openai-codex", selected_model="gpt-6-astra", billing_group="subscription"))


def _rotate(pool, reset):
    return pool.mark_exhausted_and_rotate(status_code=429, credential_id="A",
                                         error_context={"reason": "usage_limit_reached", "reset_at": reset})


def test_positive_probe_before_429_is_not_reused(tmp_path, monkeypatch):
    pool, rows, reset = _pool(tmp_path, monkeypatch)
    assert auth_codex._probe_codex_quota_restored(rows[0]["access_token"]) is True
    nxt = _rotate(pool, reset)
    assert nxt is not None and nxt.id == "B"
    assert next(e for e in pool.entries() if e.id == "A").last_status == "exhausted"


def test_singleton_token_adoption_preserves_weekly_quota(tmp_path, monkeypatch):
    pool, _, reset = _pool(tmp_path, monkeypatch)
    # Use the real singleton sync, with a newer token pair for the same device-code row.
    entry = pool._adopt(pool.entries()[0], source="device_code")
    pool._mark_exhausted(entry, 429, {"reason": "usage_limit_reached", "reset_at": reset})
    state = json.loads((tmp_path / "auth.json").read_text())
    fresh = _token("A") + "-new-generation"
    state["providers"] = {"openai-codex": {"tokens": {"access_token": fresh, "refresh_token": "new-refresh"}}}
    (tmp_path / "auth.json").write_text(json.dumps(state))
    entry = next(e for e in pool.entries() if e.id == "A")
    adopted = pool._sync_entry_from_auth_store(entry)
    assert adopted.access_token == fresh
    assert adopted.last_status == "exhausted"
    assert adopted.last_error_reset_at == reset


def test_short_window_cannot_hide_healthy_fallback(tmp_path, monkeypatch):
    pool, _, reset = _pool(tmp_path, monkeypatch)
    nxt = _rotate(pool, reset)
    assert nxt is not None and nxt.id == "B"
    _verify(pool, nxt.runtime_api_key)
    assert next(e for e in pool.entries() if e.id == "A").last_status == "exhausted"


def test_short_window_all_walled_is_retryable(tmp_path, monkeypatch):
    pool, rows, reset = _pool(tmp_path, monkeypatch, other_walled=True)
    assert _rotate(pool, reset) is None
    with pytest.raises(SubscriptionQuotaUnavailable):
        _verify(pool, rows[0]["access_token"])


@pytest.mark.parametrize("other_walled", [False, True])
def test_proven_weekly_reopen_logs_and_rotation_still_excludes_failure(tmp_path, monkeypatch, caplog, other_walled):
    reset = time.time() + 6 * 86400
    payload = {"rate_limit": {"primary_window": {"used_percent": 0},
                              "secondary_window": {"used_percent": 0, "reset_at": reset}}}
    pool, rows, _ = _pool(tmp_path, monkeypatch, payload=payload, other_walled=other_walled)
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload)
                                    if rows[0]["access_token"] in request.headers["Authorization"]
                                    else httpx.Response(429))
    monkeypatch.setattr(auth_codex, "_codex_http_client", lambda **kwargs: httpx.Client(transport=transport))
    with caplog.at_level(logging.INFO):
        nxt = _rotate(pool, reset)
    if other_walled:
        assert nxt is None
        with pytest.raises(SubscriptionQuotaUnavailable):
            _verify(pool, rows[0]["access_token"])
    else:
        assert nxt is not None and nxt.id == "B"
        _verify(pool, nxt.runtime_api_key)
    assert "bench lifted early" in caplog.text
    assert "quota_probe" in caplog.text
    assert next(e for e in pool.entries() if e.id == "A").last_status == "exhausted"


@pytest.mark.parametrize("other_walled", [False, True])
def test_rotation_exclusion_precedes_resync_and_quota_probe(tmp_path, monkeypatch, other_walled):
    pool, rows, reset = _pool(tmp_path, monkeypatch, other_walled=other_walled)
    resynced = []
    probed = []
    real_resync = pool._resync_stale_entry

    def resync(entry):
        resynced.append(entry.id)
        return real_resync(entry)

    def respond(request):
        probed.append(next(row["id"] for row in rows
                           if request.headers["Authorization"] == f"Bearer {row['access_token']}"))
        # An open short window does not reopen either weekly-walled row.
        return httpx.Response(200, json={"rate_limit": {"primary_window": {
            "used_percent": 0, "reset_at": reset - 5 * 86400}}})

    monkeypatch.setattr(pool, "_resync_stale_entry", resync)
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(auth_codex, "_codex_http_client", lambda **kwargs: httpx.Client(transport=transport))
    available, pending = pool._available_entries(clear_expired=True, exclude_id="C")
    assert resynced == ["A", "B"]
    assert probed == (["B"] if other_walled else [])
    assert [entry.id for entry in available] == (["A"] if other_walled else ["A", "B"])
    assert pending == []

    # Non-vacuity: the same excluded row really reaches both boundaries without exclusion.
    resynced.clear()
    probed.clear()
    auth_codex._codex_quota_probe_cache.clear()
    from hermes_cli.auth_codex_quota_negative import _path
    _path().unlink(missing_ok=True)  # force the live-probe non-vacuity control, not the disk throttle
    available, pending = pool._available_entries(clear_expired=True)
    assert resynced == ["A", "B", "C"]
    assert probed == (["B", "C"] if other_walled else ["C"])
    assert [entry.id for entry in available] == (["A"] if other_walled else ["A", "B"])
    assert pending == []


def test_explicit_reset_resync_logs_early_lift(tmp_path, monkeypatch, caplog):
    pool, _, reset = _pool(tmp_path, monkeypatch)
    assert _rotate(pool, reset).id == "B"
    state = json.loads((tmp_path / "auth.json").read_text())
    row = next(row for row in state["credential_pool"]["openai-codex"] if row["id"] == "A")
    row["status_cleared_at"] = time.time() + 1
    (tmp_path / "auth.json").write_text(json.dumps(state))
    with caplog.at_level(logging.INFO):
        available, _ = pool._available_entries(clear_expired=True, refresh=False)
    assert "A" in {entry.id for entry in available}
    assert "bench lifted early A mechanism=resync old_reset=" in caplog.text
