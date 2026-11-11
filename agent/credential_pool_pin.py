"""Pool-entry pinning for credential refresh: the pinned Anthropic token, and a drift warning for 401 recovery.

The pre-request credential check (``client_lifecycle._try_refresh_anthropic_client_credentials``) used to
re-resolve "the" Anthropic token as the FIRST pool entry. Once pool rotation or 401 recovery had moved the
agent to another account, every request swapped the first entry's token back in; when that token had
expired, refresh-then-retry looped on the wrong account until the refresh cap gave up on the right one.
This module reads only the pinned entry: it adopts a rotation another process persisted for it and
refreshes it when it is expiring, and never returns another account's token.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def pinned_anthropic_pool_token(pool: Any, credential_id: str) -> tuple[bool, Optional[str]]:
    """``(found, token)`` for pool entry *credential_id*.

    ``found`` is False when the pool no longer holds that entry (the caller may then re-resolve and
    rebind). ``token`` is None when the entry has no usable token (its refresh failed); the caller keeps
    the key it has and lets 401 recovery decide, rather than jumping to another account.
    """
    from agent.credential_pool import AUTH_TYPE_OAUTH

    entry = next((e for e in pool.entries() if e.id == credential_id), None)
    if entry is None:
        return False, None
    from agent.credential_pool_disabled import is_credential_disabled
    if is_credential_disabled(getattr(pool, "provider", "anthropic"), entry):
        # Disabled mid-session: report it gone so the caller re-resolves onto an enabled account.
        return False, None
    if entry.auth_type == AUTH_TYPE_OAUTH:
        # Token authority: the shared Claude Code file for the borrowed row, the pool store otherwise.
        if entry.source == "claude_code":
            entry = pool._sync_anthropic_entry_from_credentials_file(entry)
        else:
            entry = pool._sync_entry_from_pool_store(entry)
        if pool._entry_needs_refresh(entry):
            refreshed = pool._refresh_entry(entry, force=False)
            if refreshed is None:
                logger.warning(
                    "Anthropic pool entry %s is expiring and its refresh failed; keeping the current key",
                    credential_id)
                return True, None
            entry = refreshed
    token = (getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", None) or "").strip()
    return True, token or None


def warn_foreign_failing_key(pool: Any, api_key_hint: Optional[str], credential_id: Optional[str]) -> None:
    """Warn when the key that just failed belongs to a different pool entry than the one the session is
    attributed to: refreshing the attributed entry cannot fix that 401, and it spends the entry's
    single-use refresh token. This is the signature of the pre-request check drifting off the pin."""
    if not (credential_id and api_key_hint):
        return
    key_owner = next((e.id for e in pool.entries() if e.runtime_api_key == api_key_hint), None)
    if key_owner and key_owner != credential_id:
        logger.warning(
            "Credential auth failure: the failing key belongs to pool entry %s, but the session is "
            "attributed to entry %s", key_owner, credential_id)
