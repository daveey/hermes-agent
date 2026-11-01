"""Quota policy for externally minted OAuth token replacements."""
from __future__ import annotations

import time


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
