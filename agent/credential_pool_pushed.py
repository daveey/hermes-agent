"""Quota and auth policy for externally minted OAuth token replacements."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


def has_live_quota_mark(entry) -> bool:
    """A new token does not replenish an account's known quota window."""
    from agent.credential_pool import STATUS_EXHAUSTED, _parse_absolute_timestamp
    from hermes_cli.auth_codex import _is_codex_rate_limit_shaped

    reset = _parse_absolute_timestamp(entry.last_error_reset_at)
    return (
        entry.last_status == STATUS_EXHAUSTED
        and entry.last_error_code not in (401, 403)
        and reset is not None and reset > time.time()
        and _is_codex_rate_limit_shaped(
            entry.last_error_code, entry.last_error_reason, entry.last_error_message)
    )


def _auth_benched(entry) -> bool:
    from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED
    return entry.last_status == STATUS_DEAD or (
        entry.last_status == STATUS_EXHAUSTED and entry.last_error_code in (401, 403))


def _bench_predates_token(entry) -> bool:
    """The row's 401/403 was seen on an earlier token generation (``bench_token_fp``) than it holds now.

    A peer's newer pair can reach the row through the pool's own persist merge, which keeps the bench."""
    from agent.credential_pool import fingerprint_secret_value
    seen = (entry.extra or {}).get("bench_token_fp")
    return bool(seen and entry.access_token and fingerprint_secret_value(entry.access_token) != seen)


def adopt_pushed_replacements(pool: Any, model: Optional[str] = None):
    """Re-read auth.json for every auth-benched pushed row; the first one now serviceable, or None.

    A minter (the hub's cred-relay) re-minting accounts revokes the tokens every worker holds until
    its next pull. Rotation walks the in-memory pool, whose revoked rows stay benched for minutes even
    after their replacement lands on disk, so a pool whose other accounts are quota-walled reads as
    empty. For a refresh-less row the re-read is the only renewal there is.
    """
    from agent.credential_pool import _MARK_OK, STATUS_OK, CredentialPool
    from agent.credential_pool_model_cooldowns import model_cooldown_until
    if not isinstance(pool, CredentialPool):
        return None
    with pool._lock:
        for entry in list(pool._enabled_entries()):
            if not _auth_benched(entry) or entry.auth_type != "oauth" or (entry.refresh_token or "").strip():
                continue
            adopted = pool._adopt_pushed_norefresh_row(entry)
            if adopted is entry and _bench_predates_token(entry) and not has_live_quota_mark(entry):
                adopted = pool._adopt(entry, **_MARK_OK)
            if adopted is entry or adopted.last_status not in (None, STATUS_OK):
                continue
            if model_cooldown_until(adopted, model) is not None:
                continue
            pool._current_id = adopted.id
            return adopted
    return None


def auth_wall_is_transient(pool: Any) -> bool:
    """False only when every enabled entry is auth-dead after a re-read of auth.json.

    An account that is merely quota/billing-benched, cooling down for a model, or whose token was
    replaced on disk since it failed comes back without an operator, so a 401 on its sibling is not
    the configuration error that parks a Kanban card (2026-11-12 11:31Z: 31 cards gave up).
    """
    from agent.credential_pool import CredentialPool
    if not isinstance(pool, CredentialPool):
        return False
    try:
        from hermes_cli.auth import read_credential_pool
        disk = {row.get("id"): row for row in read_credential_pool(pool.provider) if isinstance(row, dict)}
    except (OSError, ValueError, RuntimeError):
        logger.debug("auth wall: auth.json re-read failed for %s", pool.provider, exc_info=True)
        disk = {}
    with pool._lock:
        entries = list(pool._enabled_entries())
    if len(entries) < 2:
        return False  # a sole credential has no sibling to wait on
    for entry in entries:
        token = (disk.get(entry.id) or {}).get("access_token")
        if isinstance(token, str) and token.strip() and token != entry.access_token:
            return True  # a replacement was pushed since this row failed
        if not _auth_benched(entry) or _bench_predates_token(entry):
            return True
    return False
