"""Refresh claims in ``agent.account_usage_cache`` can never wedge a slot for the process lifetime.

A long-lived Desktop backend rendered every pooled account ``unknown`` forever: once a slot's
refresh claim leaked (a thread start that raised, a worker that died between claims), that slot
was never refreshed again and its snapshot aged past the staleness bound. These pin the release
paths, the inflight expiry, and the read-only diagnostic that confirms the next occurrence.
"""

from __future__ import annotations

import threading

import pytest

import agent.account_usage_cache as cache
from datetime import UTC


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for store in (cache._snapshots, cache._started, cache._last_try, cache._inflight):
        store.clear()
    yield
    for store in (cache._snapshots, cache._started, cache._last_try, cache._inflight):
        store.clear()


class _BoomThread(threading.Thread):
    def start(self):
        raise RuntimeError("can't start new thread")


def _request(identity: str, provider: str = "anthropic") -> dict:
    return {"provider": provider, "identity_id": identity, "base_url": None, "api_key": f"tok-{identity}"}


def test_legacy_thread_start_failure_releases_claim(monkeypatch):
    monkeypatch.setattr(cache.threading, "Thread", _BoomThread)
    assert cache.refresh_account_usage_async(["anthropic"]) == []
    key = cache._key("anthropic")
    assert key not in cache._inflight, "a failed thread start must not leave the slot claimed"
    assert key not in cache._last_try, "a refresh that never ran must not throttle the next try"


def test_entries_thread_start_failure_releases_every_claim(monkeypatch):
    monkeypatch.setattr(cache.threading, "Thread", _BoomThread)
    assert cache.refresh_account_usage_entries_async([_request("fp:a"), _request("fp:b")]) == []
    assert not cache._inflight
    assert not cache._last_try


def test_entries_worker_releases_remaining_claims_when_a_fetch_raises(monkeypatch):
    """One fetch escaping (anything but the fetcher's own swallowed Exception) must not strand
    the slots queued behind it in the same batch."""
    from agent import account_usage

    calls: list[str] = []

    def exploding_fetch(provider, **kw):
        calls.append(kw["identity_id"])
        raise KeyboardInterrupt  # BaseException: fetch_account_usage's `except Exception` misses it

    monkeypatch.setattr(account_usage, "fetch_account_usage", exploding_fetch)
    claims = [(cache._key("anthropic", ident), _request(ident)) for ident in ("fp:a", "fp:b", "fp:c")]
    for key, _ in claims:
        assert cache._claim_refresh(key, 0.0)
    with pytest.raises(KeyboardInterrupt):
        cache._refresh_entries([request for _, request in claims])
    assert calls == ["fp:a"]
    assert not cache._inflight, f"stranded claims: {list(cache._inflight)}"


def test_entries_worker_keeps_going_after_an_ordinary_fetch_error(monkeypatch):
    from agent import account_usage

    calls: list[str] = []

    def flaky_fetch(provider, **kw):
        calls.append(kw["identity_id"])
        if kw["identity_id"] == "fp:a":
            raise RuntimeError("boom")

    monkeypatch.setattr(account_usage, "fetch_account_usage", flaky_fetch)
    requests = [_request("fp:a"), _request("fp:b")]
    for request in requests:
        assert cache._claim_refresh(cache._key("anthropic", request["identity_id"]), 0.0)
    cache._refresh_entries(requests)
    assert calls == ["fp:a", "fp:b"]
    assert not cache._inflight


def test_wedged_inflight_claim_expires(monkeypatch):
    key = cache._key("anthropic", "fp:a")
    assert cache._claim_refresh(key, 1000.0)
    # Still fresh: inside both the retry throttle and the inflight bound.
    assert not cache._claim_refresh(key, 1000.0 + cache.REFRESH_AFTER_S - 1)
    # Past the throttle but the claim is still inside the inflight bound (only if the bound is longer).
    later = 1000.0 + max(cache.REFRESH_AFTER_S, cache.INFLIGHT_EXPIRE_S) + 1
    assert cache._claim_refresh(key, later), "a claim older than INFLIGHT_EXPIRE_S must be reclaimable"


def test_inflight_expire_bound_is_a_small_multiple_of_the_fetch_timeout():
    assert cache.USAGE_FETCH_BOUND_S >= 15.0
    assert 2 * cache.USAGE_FETCH_BOUND_S <= cache.INFLIGHT_EXPIRE_S <= cache.REFRESH_AFTER_S


def test_stale_worker_release_does_not_drop_a_newer_claim():
    """A wedged worker that finally finishes after its claim expired and was retaken must not
    release the NEW claim (that would let a third refresh pile on)."""
    key = cache._key("anthropic", "fp:a")
    old = cache._claim_refresh(key, 0.0)
    new = cache._claim_refresh(key, cache.INFLIGHT_EXPIRE_S + cache.REFRESH_AFTER_S + 1)
    assert old and new and old is not new
    cache._release_refresh(key, old)
    assert key in cache._inflight
    cache._release_refresh(key, new)
    assert key not in cache._inflight


def test_worker_releases_the_key_it_claimed_even_if_home_changes(monkeypatch, tmp_path):
    """The entries worker must release the exact claimed key, not one recomputed under whatever
    home is current inside the thread."""
    from agent import account_usage

    def fetch_after_home_flip(provider, **kw):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))

    monkeypatch.setattr(account_usage, "fetch_account_usage", fetch_after_home_flip)
    threads = cache.refresh_account_usage_entries_async([_request("fp:a"), _request("fp:b")])
    for thread in threads:
        thread.join(5)
    assert not cache._inflight


def test_diagnostics_reports_inflight_and_ages_for_current_home(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

    key = cache._key("anthropic", "fp:a")
    cache._claim_refresh(key, cache.time.monotonic() - 42)
    snap = AccountUsageSnapshot(
        provider="anthropic", source="test",
        fetched_at=datetime.now(UTC) - timedelta(minutes=20),
        windows=(AccountUsageWindow(label="Session", used_percent=10.0),),
    )
    cache.remember_account_usage("anthropic", snap, identity_id="fp:b")
    # A slot belonging to another home is not reported.
    cache._inflight[("/elsewhere", "anthropic", "fp:z", "")] = (object(), 0.0)

    diag = cache.account_usage_cache_diagnostics()
    assert diag["home"] == cache._key("x")[0]
    assert diag["inflight_count"] == 1
    slots = {(s["provider"], s["identity_id"]): s for s in diag["slots"]}
    a = slots[("anthropic", "fp:a")]
    assert a["inflight"] is True and 41 <= a["inflight_age_s"] <= 60
    assert a["last_try_age_s"] is not None and a["snapshot_age_s"] is None
    b = slots[("anthropic", "fp:b")]
    assert b["inflight"] is False and b["stale"] is True and b["snapshot_age_s"] >= 20 * 60 - 5
    assert ("anthropic", "fp:z") not in slots
    assert diag["inflight_expire_s"] == cache.INFLIGHT_EXPIRE_S
    # Read-only: nothing claimed, released or dropped.
    assert len(cache._inflight) == 2
