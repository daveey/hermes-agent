"""Read-only drain telemetry. An updater's announced bound is not its actual end."""
from __future__ import annotations

import math


def collect():
    from agent import core_update_drain as drain

    records = []
    for path in drain.hold_paths():
        try:
            record = drain.read_hold(path, strict_errors=True)
        except (OSError, UnicodeError):
            return {}  # Unreadable is unknown, not evidence of no hold.
        if record is not None:
            records.append(record)
    if not records:
        return {"drain_active": 0}
    result = {"drain_active": 1}
    bounds = []
    for record in records:
        start, duration = record.get("started_at"), record.get("max_s")
        if not all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in (start, duration)):
            return result
        bound = (start + duration) * 1000
        if not math.isfinite(bound) or bound > 8.64e15:
            return result
        bounds.append(bound)
    result["drain_until_ms"] = max(bounds)
    return result


def send_compatible(send, sample):
    """Old Boards reject unknown metrics atomically; retry only additive drain fields."""
    from hermes_cli.kanban_sync_client import HubError

    try:
        return send(sample)
    except HubError as exc:
        if exc.status != 400 or not any(k in sample for k in ("drain_active", "drain_until_ms")):
            raise
        return send({k: v for k, v in sample.items() if k not in ("drain_active", "drain_until_ms")})
