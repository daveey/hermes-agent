"""Persistence-only compare-and-swap for externally verified quota recovery."""
from __future__ import annotations

import time
import logging


def clear_verified_model_cooldown(pool, observed, model: str) -> bool:
    """Clear only the exact failure/token generation that a caller verified.

    No probes or refreshes here. Read and mutate the owning store under its lock, never
    flush a pre-probe pool snapshot over a peer's refresh or another model's failure.
    """
    from agent import credential_pool as cp
    from agent.credential_pool_disabled import disabled_keys, matches
    from agent.credential_persistence import fingerprint_secret_value, is_borrowed_credential_source
    from hermes_cli import auth

    if not model or observed.provider != pool.provider:
        return False
    with pool._lock:
        target = None
        if pool.provider in cp.SINGLE_USE_REFRESH_POOL_PROVIDERS and not cp._profile_owns_pool_provider(pool.provider):
            target = cp._borrowed_single_use_pool_root()
            if target is None:
                return False
        try:
            with auth._auth_store_lock(target_path=target):
                store = auth._load_auth_store(target) if target is not None else auth._load_auth_store()
                rows = store.get("credential_pool", {}).get(pool.provider, [])
                row = next((r for r in rows if isinstance(r, dict) and r.get("id") == observed.id), None)
                if row is None:
                    return False
                current = cp.PooledCredential.from_dict(pool.provider, row)
                observed_secret = observed.runtime_api_key
                disabled = disabled_keys(pool.provider)
                if not observed_secret or matches(current, disabled) or matches(observed, disabled):
                    return False
                hydrate = False
                if current.runtime_api_key:
                    same_secret = current.runtime_api_key == observed_secret and (
                        current.access_token, current.refresh_token) == (observed.access_token, observed.refresh_token)
                else:
                    # Sanitized borrowed row: the store keeps no token, only source + fingerprint.
                    stored_fp = current.extra.get("secret_fingerprint")
                    same_secret = (is_borrowed_credential_source(current.source, pool.provider)
                                   and bool(stored_fp) and stored_fp == fingerprint_secret_value(observed_secret)
                                   and current.source == observed.source
                                   and current.extra.get("secret_source") == observed.extra.get("secret_source"))
                    hydrate = same_secret
                if not same_secret or (current.base_url, current.auth_type) != (observed.base_url, observed.auth_type):
                    return False
                status_fields = ("last_status", "last_status_at", "last_error_code", "last_error_reason",
                                 "last_error_message", "last_error_reset_at", "status_cleared_at")
                if any(getattr(current, f) != getattr(observed, f) for f in status_fields):
                    return False
                if any(current.extra.get(f) != observed.extra.get(f)
                       for f in ("cooldown_until", "failure_reason", "bench_status_at", "bench_token_fp")):
                    return False
                billing = (cp.FAILURE_REASON_BILLING, cp.FAILURE_REASON_BILLING_UNVERIFIED)
                if (current.last_status == cp.STATUS_DEAD or current.last_error_code in (401, 402, 403)
                        or current.last_error_reason in billing or current.extra.get("failure_reason") in billing):
                    return False
                cooldown = (observed.model_cooldowns or {}).get(model)
                if current.last_status == cp.STATUS_EXHAUSTED:
                    # Only Codex's explicitly observed account-wide 429 is recoverable here.
                    if pool.provider != "openai-codex" or current.last_error_code != 429:
                        return False
                    updates = {**cp._MARK_OK, "last_status_at": current.last_status_at}
                elif cooldown is not None:
                    for field in ("model_cooldowns", "model_cooldown_recorded_at", "model_cooldown_cleared_at"):
                        if (getattr(current, field) or {}).get(model) != (getattr(observed, field) or {}).get(model):
                            return False
                    cooldowns = dict(current.model_cooldowns or {})
                    cooldowns.pop(model, None)
                    cleared = {**(current.model_cooldown_cleared_at or {}), model: time.time()}
                    updates = {"model_cooldowns": cooldowns, "model_cooldown_cleared_at": cleared}
                else:
                    return False
                row.update(updates)
                auth._save_auth_store(store, target_path=target)
                adopted = cp.PooledCredential.from_dict(pool.provider, row)
                live = pool._find(lambda e: e.id == observed.id)
                persisted_pair = (adopted.access_token, adopted.refresh_token)
                if live is not None:
                    if hydrate:  # runtime secret stays in memory only; the saved row stays sanitized
                        adopted.access_token, adopted.refresh_token = observed.access_token, observed.refresh_token
                        adopted.agent_key = observed.agent_key
                    pool._replace_entry(live, adopted)
                pool._persisted_token_pairs[observed.id] = persisted_pair
                return True
        except Exception:
            # Failure to lock/read/write is not evidence of recovery.
            logging.getLogger(__name__).debug("Could not persist verified quota recovery", exc_info=True)
            return False
