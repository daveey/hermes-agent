"""Mergeable model failure generations and sticky scoped recovery receipts."""
from __future__ import annotations

import logging
from typing import Any

MODEL_STATE_FIELDS = ("model_cooldowns", "model_cooldown_recorded_at", "model_cooldown_cleared_at")


def merge_model_state(*rows: dict[str, Any]) -> dict[str, Any]:
    from agent.credential_pool import _parse_absolute_timestamp

    recorded: dict[str, float] = {}
    cleared: dict[str, float] = {}
    global_clear = max((_parse_absolute_timestamp(row.get("status_cleared_at")) or 0.0
                        for row in rows), default=0.0)
    for row in rows:
        for field, target in (("model_cooldown_recorded_at", recorded),
                              ("model_cooldown_cleared_at", cleared)):
            for model, stamp in (row.get(field) or {}).items():
                if isinstance(stamp, (int, float)):
                    target[model] = max(target.get(model, 0.0), float(stamp))
    observations: dict[str, tuple[float, float]] = {}
    for row in rows:
        for model, until in (row.get("model_cooldowns") or {}).items():
            if not isinstance(until, (int, float)):
                continue
            stamp = (row.get("model_cooldown_recorded_at") or {}).get(model, 0.0)
            receipt = max(global_clear, cleared.get(model, 0.0))
            if receipt and stamp <= receipt:
                continue
            observations[model] = max(observations.get(model, (0.0, 0.0)), (stamp, float(until)))
    result: dict[str, Any] = dict(zip(MODEL_STATE_FIELDS, (
        {model: observation[1] for model, observation in observations.items()}, recorded, cleared)))
    result = {field: value for field, value in result.items()
              if value or any(row.get(field) is not None for row in rows)}
    if global_clear:
        result["status_cleared_at"] = global_clear
    return result


def resync_model_state(pool, entry):
    """Read resets even when the credential has no account-wide failure status."""
    if not entry.model_cooldowns:
        return entry
    from hermes_cli.auth import read_credential_pool
    try:
        row = next((r for r in read_credential_pool(pool.provider) if r.get("id") == entry.id), None)
        if row is None:
            return entry
        state = merge_model_state(entry.to_dict(), row)
        if all((getattr(entry, field) or {}) == state.get(field, {}) for field in MODEL_STATE_FIELDS):
            return entry
        return pool._adopt(entry, persist=False, **state)
    except Exception:
        logging.getLogger(__name__).debug("Could not resync model cooldown state", exc_info=True)
        return entry


def adopt_persisted_row(entry, row, pair):
    """Adopt a peer's newer token pair, else only the merged model-scoped state.

    Account-wide status stays with the in-memory entry unless the pair moved: adopting a
    merged status on an unchanged (or reference-only) row would re-bench a live credential
    with a peer's or pre-relogin verdict on every ordinary flush."""
    from dataclasses import replace
    from agent.credential_pool import PooledCredential

    if row is None:
        return entry
    if any(pair) and pair != (entry.access_token, entry.refresh_token):
        return PooledCredential.from_dict(entry.provider, row)
    updates = {field: row.get(field) or None for field in MODEL_STATE_FIELDS}
    changed = any((getattr(entry, field) or None) != value for field, value in updates.items())
    return replace(entry, **updates) if changed else entry
