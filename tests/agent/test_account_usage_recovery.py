"""Fresh usage telemetry lifts a stale quota bench (early subscription reset), and nothing else.

Real auth stores on disk and the real pool; only the provider usage HTTP call is faked. Covers the
picker path (``_apply_usage`` -> per-account refresh), the background watch path
(``check_quota_recovery_once``). The gateway starter is in tests/tui_gateway.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from agent import account_usage
from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from agent.account_usage_cache import _inflight, _last_try, _snapshots, _started
from agent.account_usage_recovery import (
    check_quota_recovery_once, clear_recovered_quota_benches, start_quota_recovery_watch,
)
from agent.credential_pool import load_pool
from hermes_cli.inventory import _apply_usage

FAR = time.time() + 6 * 86400  # the provider's (now stale) reset time


def _join() -> None:
    for worker in [t for t in threading.enumerate() if "hermes-account-usage-refresh" in t.name]:
        worker.join(10)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    for store in (_snapshots, _started, _last_try, _inflight):
        store.clear()
    yield
    _join()


def _fp(token: str) -> str:
    import hashlib

    return f"fp:{hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]}"


def _write_pool(tmp_path, entries: list[dict]) -> None:
    home = tmp_path / "h"
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"version": 1, "credential_pool": {"openrouter": entries}}), encoding="utf-8")


def _entry(cred_id: str, token: str, **over) -> dict:
    entry = {"id": cred_id, "label": cred_id, "auth_type": "api_key", "priority": 0, "source": "manual", "access_token": token}
    entry.update(over)
    return entry


def _quota_bench(**over) -> dict:
    bench = {"last_status": "exhausted", "last_status_at": time.time() - 60, "last_error_code": 429,
             "last_error_reason": "usage_limit_reached", "last_error_reset_at": FAR}
    bench.update(over)
    return bench


def _snapshot(used: float, *, scope: str = "account", fetched_at: datetime | None = None) -> AccountUsageSnapshot:
    return AccountUsageSnapshot(
        provider="openrouter", source="test", fetched_at=fetched_at or datetime.now(UTC),
        windows=(AccountUsageWindow(label="Weekly", used_percent=used, reset_at=datetime.now(UTC) + timedelta(days=7), scope=scope),))


def _fetcher(monkeypatch, answers: dict) -> list:
    calls: list = []

    def fake(base_url, api_key):
        calls.append(api_key)
        return answers.get(api_key)

    monkeypatch.setitem(account_usage._USAGE_FETCHERS, "openrouter", fake)
    return calls


def _status(cred_id: str):
    return next(e for e in load_pool("openrouter").entries() if e.id == cred_id).last_status


def _rows() -> dict:
    row = {"slug": "openrouter", "name": "OpenRouter", "models": ["m"], "authenticated": True}
    _apply_usage([row])
    return {a["id"]: a for a in row["usage"]["accounts"]}


def test_picker_refresh_lifts_quota_bench_when_usage_shows_quota(monkeypatch, tmp_path):
    """Picker path: a Codex-style usage_limit_reached bench with a far reset is cleared once the
    per-account refresh sees every account window below 100%, and the next read shows it ready."""
    _write_pool(tmp_path, [_entry("c1", "tok-1", **_quota_bench()), _entry("c2", "tok-2")])
    _fetcher(monkeypatch, {"tok-1": _snapshot(0.0), "tok-2": _snapshot(20.0)})

    first = _rows()
    assert first[_fp("tok-1")]["state"] == "limited"  # cached view before the refresh lands
    _join()

    assert _status("c1") is None, "fresh usage with quota left must clear the stale quota bench"
    assert _rows()[_fp("tok-1")]["state"] == "ready"


@pytest.mark.parametrize("bench,used,scope,why", [
    (_quota_bench(), 100.0, "account", "window still spent"),
    (_quota_bench(), 0.0, "model", "only a model-scoped window: no account evidence"),
    (_quota_bench(last_error_code=401, last_error_reason="token_expired"), 0.0, "account", "auth bench"),
    (_quota_bench(last_error_code=402, last_error_reason="billing_hard_limit_reached"), 0.0, "account", "billing bench"),
    (_quota_bench(last_error_reason="rate_limit_exceeded"), 0.0, "account", "plain rate limit, not quota"),
])
def test_bench_kept_without_quota_evidence(monkeypatch, tmp_path, bench, used, scope, why):
    _write_pool(tmp_path, [_entry("c1", "tok-1", **bench), _entry("c2", "tok-2")])
    assert clear_recovered_quota_benches("openrouter", _fp("tok-1"), _snapshot(used, scope=scope)) == [], why
    assert _status("c1") == "exhausted", why


def test_snapshot_fetched_before_the_bench_proves_nothing(monkeypatch, tmp_path):
    _write_pool(tmp_path, [_entry("c1", "tok-1", **_quota_bench()), _entry("c2", "tok-2")])
    old = datetime.now(UTC) - timedelta(minutes=5)
    assert clear_recovered_quota_benches("openrouter", _fp("tok-1"), _snapshot(0.0, fetched_at=old)) == []
    assert _status("c1") == "exhausted"


def test_only_the_matching_account_is_cleared(monkeypatch, tmp_path):
    _write_pool(tmp_path, [_entry("c1", "tok-1", **_quota_bench()), _entry("c2", "tok-2", **_quota_bench())])
    assert clear_recovered_quota_benches("openrouter", _fp("tok-1"), _snapshot(5.0)) == ["c1"]
    assert _status("c1") is None and _status("c2") == "exhausted"


def test_background_watch_fetches_only_benched_accounts_and_clears_recovered(monkeypatch, tmp_path):
    """Watch path: no picker open. One tick fetches usage for quota-benched entries only (not the
    healthy one, not a lapsed bench) and lifts the bench whose account has quota again."""
    _write_pool(tmp_path, [
        _entry("c1", "tok-1", **_quota_bench()),
        _entry("c2", "tok-2"),
        _entry("c3", "tok-3", **_quota_bench(last_error_reset_at=time.time() - 10)),
        _entry("c4", "tok-4", **_quota_bench()),
    ])
    calls = _fetcher(monkeypatch, {"tok-1": _snapshot(0.0), "tok-4": _snapshot(100.0)})

    for worker in check_quota_recovery_once():
        worker.join(10)

    assert sorted(calls) == ["tok-1", "tok-4"]
    assert _status("c1") is None
    assert _status("c4") == "exhausted"


def test_watch_starts_once_and_ticks(monkeypatch):
    import agent.account_usage_recovery as recovery

    ticked = threading.Event()
    monkeypatch.setattr(recovery, "_watch_started", False)
    monkeypatch.setattr(recovery, "check_quota_recovery_once", lambda: ticked.set() or [])
    assert start_quota_recovery_watch(interval_s=0.01) is True
    assert start_quota_recovery_watch(interval_s=0.01) is False
    assert ticked.wait(2), "the watch thread must run a recovery check each interval"

