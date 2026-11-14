"""Conservative evidence for lifting a persisted Codex quota window."""
import math


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def quota_restored(payload, reset_at=None):
    """A partial short-window response cannot disprove a named long-window wall.

    Match absolute reset epochs with a minute of provider rounding tolerance.
    Unknown/missing evidence keeps the bench; account/model denial always wins.
    """
    limits = [payload.get("rate_limit") or {}] + [
        extra.get("rate_limit") or {} for extra in (payload.get("additional_rate_limits") or [])
        if isinstance(extra, dict)]
    usages = []
    matched = reset_at is None
    for limit in limits:
        if limit.get("allowed") is False or limit.get("limit_reached") is True:
            return False
        for key in ("primary_window", "secondary_window"):
            window = limit.get(key) or {}
            used = window.get("used_percent")
            if _number(used):
                usages.append(used)
                reset = window.get("reset_at")
                if _number(reset_at) and _number(reset) and abs(reset - reset_at) <= 60:
                    matched = True
    return bool(usages) and matched and all(0 <= used < 100 for used in usages)


def clear_proven_cooldown(proven):
    """Clear only the observed row/generation, never other accounts or a newer 429."""
    import logging
    from agent.credential_pool import _borrowed_single_use_pool_root, _profile_owns_pool_provider, _parse_absolute_timestamp
    from hermes_cli.auth import _auth_store_lock, _load_auth_store, _save_auth_store
    from hermes_cli.auth_codex import _codex_pool_dicts, _pool_entries, _entry_is_rate_limit_exhausted, _clear_pool_entry_status
    target = None if _profile_owns_pool_provider("openai-codex") else _borrowed_single_use_pool_root()
    with _auth_store_lock(target_path=target):
        store = _load_auth_store(target)
        for row in _codex_pool_dicts(_pool_entries(store, "openai-codex")):
            if (row.get("id") != proven.get("id") or row.get("last_status_at") != proven.get("last_status_at")
                    or _parse_absolute_timestamp(row.get("last_error_reset_at")) != proven.get("reset_at")
                    or row.get("access_token") != proven.get("access_token")
                    or not _entry_is_rate_limit_exhausted(row)):
                continue
            logging.getLogger(__name__).info(
                "credential pool: bench lifted early %s mechanism=singleton_quota_probe old_reset=%s",
                row.get("label") or row.get("id"), proven.get("reset_at"))
            _clear_pool_entry_status(row)
            _save_auth_store(store, target_path=target)
            return True
    return False
