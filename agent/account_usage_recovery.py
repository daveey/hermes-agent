"""Clear a quota bench once fresh usage telemetry shows the account has quota again.

A provider quota error (Codex ``usage_limit_reached``) benches a pool credential until the reset
time the provider sent with it. When the subscription is reset early (a plan change, a new billing
cycle, a manual reset on the provider's site) nothing would notice: the bench held the account out
of rotation, and the usage chip red, until that old reset time.

This module is the one place usage telemetry may change routing, and only in the safe direction:
it clears a bench, never sets one. A wrong clear costs one request, whose quota error benches the
credential again. The rule is narrow on purpose:

- the entry is ``exhausted`` for a usage/quota reason code (not billing, not auth, not a plain 429);
- the snapshot was fetched AFTER the bench was set, for the same account identity;
- the snapshot has at least one account-scoped window, and every one of them is below 100%.

``start_quota_recovery_watch`` re-fetches usage for benched accounts on a timer, so recovery is
seen even when no picker is open (a picker read already refreshes every entry it lists).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Provider reason codes that mean "this account's usage window is spent". Billing (402 / credits)
# is left out on purpose: usage windows with room say nothing about a payment problem.
QUOTA_BENCH_REASONS = frozenset({
    "usage_limit_reached", "usage_limit", "insufficient_quota", "quota_exceeded", "quota_exhausted",
    "rate_limit_exceeded_quota",
})

QUOTA_RECOVERY_INTERVAL_S = 10 * 60.0
_EXHAUSTED_WINDOW_PERCENT = 100.0

_watch_lock = threading.Lock()
_watch_started = False


def is_quota_bench(entry: Any) -> bool:
    """Whether *entry* is benched because its provider said the usage quota is spent."""
    from agent.credential_pool import STATUS_EXHAUSTED

    if getattr(entry, "last_status", None) != STATUS_EXHAUSTED:
        return False
    if getattr(entry, "last_error_code", None) in (401, 402):
        return False
    if str(getattr(entry, "failure_reason", "") or "").strip().lower() in {"billing", "auth", "auth_permanent"}:
        return False
    return str(getattr(entry, "last_error_reason", "") or "").strip().lower() in QUOTA_BENCH_REASONS


def snapshot_shows_quota(snapshot: Any) -> bool:
    """At least one account-scoped window, and none of them spent."""
    if snapshot is None or getattr(snapshot, "unavailable_reason", None):
        return False
    windows = [w for w in (getattr(snapshot, "windows", ()) or ())
               if getattr(w, "scope", "account") == "account" and getattr(w, "used_percent", None) is not None]
    return bool(windows) and all(float(w.used_percent) < _EXHAUSTED_WINDOW_PERCENT for w in windows)


def _fetched_epoch(snapshot: Any) -> Optional[float]:
    fetched = getattr(snapshot, "fetched_at", None)
    try:
        return fetched.timestamp() if fetched is not None else None
    except Exception:
        return None


def clear_recovered_quota_benches(provider: str, identity_id: Optional[str], snapshot: Any) -> list[str]:
    """Clear the quota bench of every pool entry of *identity_id*'s account that *snapshot* shows
    has quota again. Returns the cleared entry ids. Never raises; never sets a bench."""
    if not provider or snapshot is None:
        return []
    identity = getattr(snapshot, "identity", None) or identity_id
    fetched = _fetched_epoch(snapshot)
    if not identity or fetched is None or not snapshot_shows_quota(snapshot):
        return []
    try:
        from agent.account_usage_cache import _identity_id_for
        from agent.credential_pool import load_pool

        pool = load_pool(provider)
        cleared: list[str] = []
        for entry in list(pool.entries()):
            if not is_quota_bench(entry):
                continue
            if _identity_id_for(provider, entry) != identity:
                continue
            benched_at = getattr(entry, "last_status_at", None)
            if not benched_at or fetched <= float(benched_at):
                continue  # telemetry from before the bench proves nothing about recovery
            if pool.reset_status(entry.id) is not None:
                cleared.append(entry.id)
                logger.info("account usage: cleared quota bench on %s credential %s; fresh usage shows quota left",
                            provider, getattr(entry, "label", "") or entry.id)
        return cleared
    except Exception:
        logger.debug("account usage: quota recovery check failed for %s", provider, exc_info=True)
        return []


def benched_usage_requests(providers: Optional[Iterable[str]] = None) -> list[dict]:
    """Per-account usage refresh requests for quota-benched entries whose bench is still live."""
    from agent.account_usage_cache import _identity_id_for, has_account_usage
    from agent.credential_pool import _exhausted_until, load_pool
    from hermes_cli.auth import read_credential_pool
    from hermes_cli.inventory import _pool_entry_route_base_url

    now = time.time()
    slugs = list(providers) if providers is not None else [s for s, rows in read_credential_pool().items() if rows]
    requests: list[dict] = []
    for slug in slugs:
        try:
            if not has_account_usage(slug):
                continue
            seen: set[str] = set()
            for entry in load_pool(slug).entries():
                if not is_quota_bench(entry):
                    continue
                until = _exhausted_until(entry)
                if until is not None and until <= now:
                    continue  # the bench has already lapsed; routing will retry it
                identity = _identity_id_for(slug, entry)
                if identity in seen:
                    continue
                seen.add(identity)
                requests.append({"provider": slug, "identity_id": identity,
                                 "base_url": _pool_entry_route_base_url(slug, entry),
                                 "api_key": entry.runtime_api_key})
        except Exception:
            logger.debug("account usage: benched-entry scan failed for %s", slug, exc_info=True)
    return requests


def check_quota_recovery_once() -> list[threading.Thread]:
    """Queue usage refreshes for every quota-benched account; the refresh clears what recovered."""
    from agent.account_usage_cache import refresh_account_usage_entries_async

    requests = benched_usage_requests()
    return refresh_account_usage_entries_async(requests) if requests else []


def start_quota_recovery_watch(interval_s: float = QUOTA_RECOVERY_INTERVAL_S) -> bool:
    """Start the process-wide daemon that checks benched accounts every *interval_s*. Idempotent."""
    global _watch_started
    with _watch_lock:
        if _watch_started:
            return False
        _watch_started = True

    def _loop() -> None:
        while True:
            time.sleep(interval_s)
            try:
                check_quota_recovery_once()
            except Exception:
                logger.debug("account usage: quota recovery watch tick failed", exc_info=True)

    threading.Thread(target=_loop, name="account-usage-quota-recovery", daemon=True).start()
    return True
