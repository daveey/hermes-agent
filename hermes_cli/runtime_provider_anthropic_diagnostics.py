"""Non-secret diagnostics when Anthropic token resolution found no usable token."""
from __future__ import annotations


import json
from pathlib import Path


def _entries_at(path: Path | None) -> list:
    if path is None:
        return []
    try:
        store = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    if not isinstance(store, dict):
        raise ValueError("invalid credential store")
    pool = store.get("credential_pool", {})
    if not isinstance(pool, dict):
        raise ValueError("invalid credential pool")
    entries = pool.get("anthropic", [])
    if not isinstance(entries, list):
        raise ValueError("invalid provider pool")
    return entries


def _pool_entries() -> list:
    # Reuse canonical profile/root paths and provider-level precedence, but not
    # the general loader: it backs up corrupt files and masks fallback errors.
    from hermes_cli.auth import _auth_file_path, _global_auth_file_path
    return _entries_at(_auth_file_path()) or _entries_at(_global_auth_file_path())


def unavailable_pool_message(missing_message: str) -> str:
    """Read persisted statuses, never refresh, select, clear cooldowns or expose row details.

    A failed token resolution proves unavailability, not absence. In particular,
    the resolver filters globally exhausted entries even without a target model.
    """
    try:
        entries = _pool_entries()
    except (OSError, ValueError):
        return ("Anthropic credentials are unavailable and the existing credential pool could not be inspected. "
                "Check the provider/profile credential store before requesting new authentication.")
    if not entries:
        return missing_message
    statuses = {entry.get("last_status") if isinstance(entry, dict) and isinstance(entry.get("last_status"), str)
                else None for entry in entries}
    if statuses and statuses <= {"exhausted", "dead"} and "exhausted" in statuses:
        return ("Existing Anthropic credentials are marked rate-limited; no usable token is available. "
                "Inspect the existing pool and reset times with `hermes auth list`; wait for capacity "
                "or use an explicitly authorized route. Keep explicit model/provider pins unchanged.")
    if statuses == {"dead"}:
        return ("Existing Anthropic credentials are marked invalid or revoked. Inspect `hermes auth list`; "
                "re-authenticate the affected account with `hermes auth add anthropic` if required.")
    return ("Existing Anthropic credentials were found but no usable token resolved. "
            "Inspect `hermes auth list` and the active provider/profile before requesting new authentication; "
            "do not clear cooldowns or change explicit model/provider pins.")
