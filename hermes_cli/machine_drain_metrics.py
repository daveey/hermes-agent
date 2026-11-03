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


# Fields newer Boards accept and older Boards reject (400, atomically). CPU busy fields are never resent: the
# compatibility retry strips them, so an old CPU sample is never re-stamped as fresh.
ADDITIVE_FIELDS = ("drain_active", "drain_until_ms", "cpu_busy_pct", "cpu_busy_window_ms", "cpu_busy_sample_end_ms")


def send_compatible(send, sample):
    """Old Boards reject unknown metrics atomically; retry once with every additive field removed.

    Only a 400 on a sample that carried an additive field is retried; any other error (timeouts included) is raised
    and the sample is dropped. An unrelated validation error simply fails again on the retry and is raised."""
    from hermes_cli.kanban_sync_client import HubError

    try:
        return send(sample)
    except HubError as exc:
        if exc.status != 400 or not any(k in sample for k in ADDITIVE_FIELDS):
            raise
        return send({k: v for k, v in sample.items() if k not in ADDITIVE_FIELDS})
