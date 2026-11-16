"""What ``model.options`` knows about subscription usage without waiting on the network.

Usage windows come from a provider API (:func:`agent.account_usage.fetch_account_usage`), so a
picker reads this cache and asks for a background refresh instead of fetching inline. Every fetch
lands here, including the one ``session.usage`` runs after each turn, so a picker is at most one
turn or one refresh interval behind.

Slots are keyed per profile home AND per account: one process may serve many profiles, and a
provider may have a pool of credentials whose usage must not contaminate each other. A fetcher
that can identify its account (a decoded Codex JWT principal) dedupes several credentials of one
account onto a single slot; fetchers that cannot get one slot per credential, never a
provider-wide singleton. The legacy provider slot (``identity_id=None``) remains the single-account
gauge: a per-turn or ``/usage`` fetch still lands there, so non-pooled chips are unchanged.
"""

from __future__ import annotations

import logging
import threading
import time
from contextvars import copy_context
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from agent.account_usage import AccountUsageSnapshot

# A picker opened again within this long reuses what it has; usage windows move in minutes, not seconds.
REFRESH_AFTER_S = 120.0
# A snapshot older than this renders as state ``unknown`` instead of a confident gauge: a benched
# credential's windows stop updating, and reporting stale numbers as live would mislabel an
# account that may have recovered (or worsened) in the meantime.
SNAPSHOT_STALE_AFTER_S = 15 * 60.0

# Built-in fetchers bound each HTTP call at 15 s (httpx), plugin hooks at 10 s; a Codex read may try
# two backend URLs. A refresh still claimed after twice that is wedged, not slow.
USAGE_FETCH_BOUND_S = 15.0
# An inflight claim older than this no longer blocks a new refresh. A leaked claim (a thread start
# that raised, a worker that died before its ``finally``) would otherwise hide the slot's usage for
# the life of the process: the snapshot ages past ``SNAPSHOT_STALE_AFTER_S`` and renders
# ``unknown`` forever. A duplicate fetch after expiry is harmless (the late-fetch guard orders it).
INFLIGHT_EXPIRE_S = 4 * USAGE_FETCH_BOUND_S

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# slot key (home, provider, identity_id, base_url) → snapshot
_snapshots: dict[tuple[str, str, str, str], AccountUsageSnapshot] = {}
# slot key → monotonic time the stored snapshot's fetch STARTED (late-fetch guard)
_started: dict[tuple[str, str, str, str], float] = {}
# slot key → monotonic time of the last refresh try (success or failure)
_last_try: dict[tuple[str, str, str, str], float] = {}
# slot key → (claim token, monotonic time the claim was taken or last touched by its worker)
_inflight: dict[tuple[str, str, str, str], tuple[object, float]] = {}


_generation = [0]


def snapshot_generation() -> int:
    """Counter bumped each time a snapshot lands; memos of payloads that embed usage key on it."""
    return _generation[0]


def _key(provider: str, identity_id: Optional[str] = None, base_url: Optional[str] = None) -> tuple[str, str, str, str]:
    from hermes_constants import hermes_home_key

    return (
        hermes_home_key(),
        str(provider or "").strip().lower(),
        str(identity_id or "").strip(),
        str(base_url or "").strip().rstrip("/"),
    )


def has_account_usage(provider: str) -> bool:
    """Whether *provider* can report usage windows at all (a built-in fetcher or a plugin hook)."""
    from agent.account_usage import _USAGE_FETCHERS
    from providers import get_provider_profile
    from providers.base import ProviderProfile

    slug = str(provider or "").strip().lower()
    if slug in _USAGE_FETCHERS:
        return True
    profile = get_provider_profile(slug) if slug else None
    return profile is not None and type(profile).fetch_account_usage is not ProviderProfile.fetch_account_usage


def _identity_id_for(provider: str, entry: Any) -> str:
    """Stable non-secret account id of one pool entry, for slot keys and wire ``accounts[].id``.

    A trusted decoded identity (Codex JWT principal) wins: several credentials of one account
    share a slot, so the account is counted once. Without one, the entry's token fingerprint
    stands in — same token ⇒ same account, and a replaced token never reuses the old quota.
    A metadata-only row (no token) falls back to the entry id: nothing better is knowable.
    """
    token = str(getattr(entry, "runtime_api_key", "") or "").strip()
    if not token:
        return f"entry:{getattr(entry, 'id', '')}"
    if provider == "openai-codex":
        from agent.credential_pool import _codex_principal_identity

        principal = _codex_principal_identity(token)
        if principal:
            return f"codex:{principal[0]}:{principal[1]}"
    import hashlib

    return f"fp:{hashlib.sha256(token.encode('utf-8')).hexdigest()[:16]}"


def remember_account_usage(
    provider: Optional[str], snapshot: Optional[AccountUsageSnapshot],
    *, identity_id: Optional[str] = None, base_url: Optional[str] = None,
    started_monotonic: Optional[float] = None,
) -> None:
    """Land one fetch result.

    ``identity_id`` given (per-credential fetch): the snapshot is stored under that account's
    slot only — one pool entry's usage must never pose as the provider-wide gauge. The snapshot's
    own trusted ``identity`` (decoded from the very token it was fetched with) wins over the
    caller's id, so a replaced credential cannot repopulate its old account's slot.

    ``identity_id`` absent (per-turn / ``/usage`` fetch): stored in the provider's legacy slot —
    the single-account gauge — and additionally in the account's identity slot when the fetcher
    identified it, so a pooled picker sees per-turn data without waiting on its own refresh.

    A snapshot without windows is never stored: a failed or empty fetch must not erase a fresher
    snapshot. A fetch that STARTED before the freshest stored one cannot replace it either
    (``started_monotonic``), however late its response arrives.
    """
    if not provider or snapshot is None or not snapshot.windows:
        return
    started = time.monotonic() if started_monotonic is None else started_monotonic
    with _lock:
        targets = []
        snapshot_identity = getattr(snapshot, "identity", None)
        if identity_id is None:
            targets.append(_key(provider, None, base_url))
            if snapshot_identity:
                targets.append(_key(provider, snapshot_identity, None))
        else:
            targets.append(_key(provider, snapshot_identity or identity_id, None))
        for key in targets:
            if key in _started and _started[key] > started:
                continue  # a fresher fetch already landed; this late result must not replace it
            _snapshots[key] = snapshot
            _started[key] = started
            _generation[0] += 1


def cached_account_usage(
    provider: str, *, identity_id: Optional[str] = None, base_url: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    with _lock:
        return _snapshots.get(_key(provider, identity_id, base_url))


def snapshot_is_stale(snapshot: Optional[AccountUsageSnapshot]) -> bool:
    """True when the snapshot is too old to render as a live gauge (the picker shows state
    ``unknown`` instead of trusting its percentages)."""
    from agent.account_usage import AccountUsageSnapshot as _Snapshot

    if not isinstance(snapshot, _Snapshot):
        return True
    try:
        age = time.time() - snapshot.fetched_at.timestamp()
    except (AttributeError, TypeError, ValueError, OSError, OverflowError):
        return True
    return age < 0 or age > SNAPSHOT_STALE_AFTER_S


def _claim_refresh(key: tuple[str, str, str, str], now: float) -> Optional[object]:
    """Take the refresh right for *key* unless one is inflight or tried too recently.

    Returns the claim token (truthy) or ``None``. A failed or empty fetch still counts as a try,
    so a dead credential (or a provider with no usage API) isn't re-asked on every picker open.
    An inflight claim older than ``INFLIGHT_EXPIRE_S`` is presumed leaked and taken over."""
    with _lock:
        held = _inflight.get(key)
        if held is not None:
            age = now - held[1]
            if age < INFLIGHT_EXPIRE_S:
                return None
            logger.warning(
                "account usage: refresh claim for %s/%s expired after %.0fs inflight; reclaiming",
                key[1], key[2] or "-", age)
        if now - _last_try.get(key, float("-inf")) < REFRESH_AFTER_S:
            return None
        token = object()
        _inflight[key] = (token, now)
        _last_try[key] = now
        return token


def _touch_refresh(key: tuple[str, str, str, str], token: Optional[object]) -> None:
    """Restart *key*'s inflight clock as its worker begins the fetch, so a slot queued behind
    serial siblings is not mistaken for a wedged one."""
    with _lock:
        held = _inflight.get(key)
        if held is not None and (token is None or held[0] is token):
            _inflight[key] = (held[0], time.monotonic())


def _release_refresh(key: tuple[str, str, str, str], token: Optional[object] = None) -> None:
    """Drop *key*'s claim. With *token*, only that exact claim: a worker whose claim expired and
    was retaken must not release the newer one."""
    with _lock:
        held = _inflight.get(key)
        if held is not None and (token is None or held[0] is token):
            del _inflight[key]


def _abandon_refresh(key: tuple[str, str, str, str], token: Optional[object]) -> None:
    """Undo a claim whose worker never started: release it and forget the try, so the next
    picker open retries instead of waiting out ``REFRESH_AFTER_S`` for a fetch that never ran."""
    with _lock:
        held = _inflight.get(key)
        if held is not None and (token is None or held[0] is token):
            del _inflight[key]
            _last_try.pop(key, None)


def refresh_account_usage_async(providers: Iterable[str]) -> list[threading.Thread]:
    """Background-refresh the provider-wide (legacy) slot for each provider not tried within
    ``REFRESH_AFTER_S``. Returns the started threads (daemon; callers never join them)."""
    started: list[threading.Thread] = []
    now = time.monotonic()
    for provider in dict.fromkeys(providers):
        key = _key(provider)
        token = _claim_refresh(key, now)
        if token is None:
            continue
        try:
            thread = threading.Thread(
                target=copy_context().run, args=(_refresh_legacy, provider, key, token),
                name="hermes-account-usage-refresh", daemon=True)
            thread.start()
        except Exception as exc:  # e.g. RuntimeError("can't start new thread") under load
            _abandon_refresh(key, token)
            logger.warning("account usage: could not start refresh for %s: %s", provider, exc)
            continue
        started.append(thread)
    return started


def _refresh_legacy(provider: str, key: tuple[str, str, str, str], token: Optional[object] = None) -> None:
    from agent.account_usage import fetch_account_usage

    try:
        _touch_refresh(key, token)
        fetch_account_usage(provider)  # remembers its own result
    finally:
        _release_refresh(key, token)


def refresh_account_usage_entries_async(requests: Iterable[dict]) -> list[threading.Thread]:
    """Background-refresh per-credential usage slots. *requests* entries are
    ``{"provider", "identity_id", "base_url", "api_key"}`` — the live credentials to read.

    One worker thread fetches all due entries serially (no thread storm for a 10-credential
    pool), read-only: a 401 or any other failure is reported, never repaired by rotating or
    refreshing the credential. Throttled per account slot, failures included."""
    due: list[dict] = []
    now = time.monotonic()
    try:
        for request in requests:
            provider = str(request.get("provider") or "").strip().lower()
            identity_id = str(request.get("identity_id") or "").strip()
            if not provider or not identity_id or not str(request.get("api_key") or "").strip():
                continue
            key = _key(provider, identity_id)
            token = _claim_refresh(key, now)
            if token is None:
                continue
            # The worker releases exactly this key/claim; it never re-derives the key in-thread.
            due.append({**request, "provider": provider, "identity_id": identity_id,
                        "_claim_key": key, "_claim_token": token})
        if not due:
            return []
        thread = threading.Thread(
            target=copy_context().run, args=(_refresh_entries, list(due)),
            name="hermes-account-usage-refresh", daemon=True)
        thread.start()
    except Exception as exc:
        for request in due:
            _abandon_refresh(request["_claim_key"], request["_claim_token"])
        logger.warning("account usage: could not start per-account refresh (%d slots): %s", len(due), exc)
        return []
    return [thread]


def _refresh_entries(requests: list[dict]) -> None:
    from agent.account_usage import fetch_account_usage
    from agent.account_usage_recovery import clear_recovered_quota_benches

    claims = [(request.get("_claim_key") or _key(request["provider"], request["identity_id"]),
               request.get("_claim_token")) for request in requests]
    try:
        for request, (key, token) in zip(requests, claims):
            try:
                _touch_refresh(key, token)
                snapshot = fetch_account_usage(
                    request["provider"], base_url=request.get("base_url"), api_key=request.get("api_key"),
                    read_only=True, identity_id=request["identity_id"],
                )
                # Fresh windows with quota left lift a stale quota bench (early reset, plan change).
                clear_recovered_quota_benches(request["provider"], request["identity_id"], snapshot)
            except Exception as exc:  # one bad slot must not starve its siblings
                logger.debug("account usage: refresh for %s/%s failed: %s",
                             request.get("provider"), request.get("identity_id"), exc)
            finally:
                _release_refresh(key, token)
    finally:
        # Anything escaping the loop (a BaseException) must not strand the slots behind it.
        for key, token in claims:
            _release_refresh(key, token)


def account_usage_cache_diagnostics(*, now: Optional[float] = None) -> dict:
    """Read-only view of this process's usage cache for the CURRENT home: which slots hold a
    refresh claim and for how long, when each was last tried, and how old its snapshot is.
    Confirms (or rules out) a wedged claim behind accounts rendering ``unknown``."""
    mono = time.monotonic() if now is None else now
    wall = time.time()
    home = _key("")[0]

    def _age(value: Optional[float]) -> Optional[float]:
        return None if value is None else round(mono - value, 1)

    with _lock:
        keys = {k for store in (_snapshots, _last_try, _inflight) for k in store if k[0] == home}
        slots = []
        for key in sorted(keys):
            held = _inflight.get(key)
            snapshot = _snapshots.get(key)
            try:
                snapshot_age = round(wall - snapshot.fetched_at.timestamp(), 1) if snapshot else None
            except (AttributeError, TypeError, ValueError, OSError, OverflowError):
                snapshot_age = None
            slots.append({
                "provider": key[1], "identity_id": key[2] or None, "base_url": key[3] or None,
                "inflight": held is not None,
                "inflight_age_s": _age(held[1]) if held else None,
                "last_try_age_s": _age(_last_try.get(key)),
                "snapshot_age_s": snapshot_age,
                "stale": snapshot_is_stale(snapshot),
            })
        inflight_count = sum(1 for k in _inflight if k[0] == home)
    return {
        "home": home, "inflight_count": inflight_count, "slots": slots,
        "refresh_after_s": REFRESH_AFTER_S, "inflight_expire_s": INFLIGHT_EXPIRE_S,
        "snapshot_stale_after_s": SNAPSHOT_STALE_AFTER_S,
    }
