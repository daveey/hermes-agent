"""Disk-vs-memory status merge for credential-pool rows (split out of ``hermes_cli.auth``).

Re-exported by ``hermes_cli.auth``; see ``write_credential_pool`` there for the writer side.
"""

from __future__ import annotations

import time
from typing import Any, Optional

_POOL_STATUS_FIELDS = (
    "last_status", "last_status_at", "last_error_code", "last_error_reason", "last_error_message",
    "last_error_reset_at", "status_cleared_at")


def _merge_disk_cooldown_state(
    entry: dict[str, Any], disk_entry: Optional[dict[str, Any]], provider_id: str,
) -> dict[str, Any]:
    """Keep a newer on-disk cooldown/quarantine over a stale in-memory one.

    ``write_credential_pool`` persists an in-memory snapshot that may predate another process
    marking the same credential exhausted/dead; without this merge the later rewrite resurrects a
    rate-limited key as healthy and both processes resume hammering it. The mirror image is a
    ``hermes auth reset`` that postdates the snapshot's cooldown (``status_cleared_at`` newer than
    its ``last_status_at``): the disk row wins there too, or a live session's next ordinary flush
    would write the reset cooldown straight back (#89415)."""
    if not isinstance(disk_entry, dict):
        return entry
    try:
        from agent.credential_pool import (
            PooledCredential, STATUS_DEAD, STATUS_EXHAUSTED, STATUS_OK, _exhausted_until,
            _parse_absolute_timestamp,
        )

        # Model cooldowns are independent observations: keep the latest reset per model so a
        # writer that just cooled one model cannot erase another process's cooldown for another.
        from agent.credential_pool_model_cooldowns import merge_model_cooldowns
        merged_cooldowns = merge_model_cooldowns(disk_entry.get("model_cooldowns"), entry.get("model_cooldowns"))
        merged = {**entry, "model_cooldowns": merged_cooldowns} if merged_cooldowns else entry
        disk_status_fields = {f: disk_entry.get(f) for f in _POOL_STATUS_FIELDS}

        mem_ts = _parse_absolute_timestamp(entry.get("last_status_at")) or 0.0
        cleared_ts = _parse_absolute_timestamp(disk_entry.get("status_cleared_at")) or 0.0
        if entry.get("last_status") in (STATUS_DEAD, STATUS_EXHAUSTED) and cleared_ts > mem_ts:
            return {**merged, **disk_status_fields}
        disk_status = disk_entry.get("last_status")
        # An ok row stamped with a failure generation records a verified recovery from that
        # exhaustion (the Codex quota probe keeps the superseded ``last_status_at``). A stale
        # snapshot of that same exhaustion, or an older one, must not write it back; a 429
        # stamped later than the recovered generation still wins below as usual.
        disk_ok_ts = _parse_absolute_timestamp(disk_entry.get("last_status_at")) or 0.0
        if (entry.get("last_status") == STATUS_EXHAUSTED and disk_status == STATUS_OK
                and mem_ts and disk_ok_ts >= mem_ts):
            return {**merged, **disk_status_fields}
        if disk_status not in (STATUS_DEAD, STATUS_EXHAUSTED):
            return merged
        # A token change means the caller re-authed this entry and intentionally cleared its status:
        # never resurrect the old cooldown onto fresh credentials.
        mem_access = entry.get("access_token") or ""
        disk_access = disk_entry.get("access_token") or ""
        if mem_access and disk_access and mem_access != disk_access:
            return entry
        disk_ts = _parse_absolute_timestamp(disk_entry.get("last_status_at")) or 0.0
        if disk_ts <= mem_ts:
            return merged
        if disk_status == STATUS_EXHAUSTED:
            until = _exhausted_until(PooledCredential.from_dict(provider_id, disk_entry))
            if until is None or until <= time.time():
                return merged
        return {**merged, **disk_status_fields}
    except Exception:  # pragma: no cover - best-effort merge
        return entry
