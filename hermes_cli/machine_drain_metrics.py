"""Read-only drain telemetry. An updater's announced bound is not its actual end."""
from __future__ import annotations

import math
from hermes_cli.machine_admission_metrics import FIELDS as ADMISSION_FIELDS


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
# compatibility retry strips them, so an old CPU sample is never re-stamped as fresh. ``claiming`` is newer than
# these and has its own downgrade so a Board that predates it does not discard drain, admission or counters.
ADDITIVE_FIELDS = ("drain_active", "drain_until_ms", "cpu_busy_pct", "cpu_busy_window_ms", "cpu_busy_sample_end_ms") + ADMISSION_FIELDS


def send_compatible(send, sample, on_counters_rejected=None, on_claiming_rejected=None):
    """Old Boards reject unknown metrics atomically; downgrade each optional generation once.

    Only a 400 on a sample that carried an additive field is retried; any other error (timeouts included) is raised
    and the sample is dropped. An unrelated validation error simply fails again on the retry and is raised.

    Throughput counters (``machine_throughput_metrics.FIELDS``) are newer still: a Board that predates them answers
    400 "unknown field(s): ...". That answer first drops just the counters (calling ``on_counters_rejected`` so the
    caller stops sending them for a while) and the CPU busy fields, keeping drain fields for a Board that accepts
    those. ``tokens_by_model`` is dropped before the scalar counters, preserving
    those counters for Boards that support the scalars. ``claiming`` is newer than both: a Board that
    rejects it drops only that field, then the older chain runs. Older counter/drain fallbacks
    remain available if that downgraded request is also rejected."""
    from hermes_cli.kanban_sync_client import HubError
    from hermes_cli.machine_throughput_metrics import FIELDS as COUNTERS

    from hermes_cli.machine_persistent_jobs import fresh
    sample = fresh(sample)
    try:
        return send(sample)
    except HubError as exc:
        if exc.status != 400:
            raise
        # A rejected safety observation is UNKNOWN on every downgrade, not a
        # reason to disable unrelated throughput counters. Never replay its zero.
        persistent = ("persistent_jobs", "persistent_jobs_at_ms")
        if any(k in sample for k in persistent):
            from hermes_cli.machine_cpu_metrics import FIELDS as CPU
            return send_compatible(send, {k: v for k, v in sample.items()
                                          if k not in persistent and k not in CPU},
                                    on_counters_rejected, on_claiming_rejected)
        if "claiming" in sample:
            # Newest field. Remember the rejection so the next fresh report can
            # omit it and keep a CPU sample the Board already accepts.
            if on_claiming_rejected is not None:
                on_claiming_rejected()
            from hermes_cli.machine_cpu_metrics import FIELDS as CPU
            return send_compatible(send, {k: v for k, v in sample.items()
                                          if k != "claiming" and k not in CPU},
                                    on_counters_rejected, on_claiming_rejected)
        if "tokens_by_model" in sample:
            # A scalar-capable Board may predate the model map. Keep its counters
            # enabled, and never restamp the already attempted CPU sample.
            from hermes_cli.machine_cpu_metrics import FIELDS as CPU
            return send_compatible(send, {k: v for k, v in sample.items()
                                          if k != "tokens_by_model" and k not in CPU},
                                    on_counters_rejected, on_claiming_rejected)
        if any(k in sample for k in COUNTERS) and "unknown field" in str(exc):
            if on_counters_rejected is not None:
                on_counters_rejected()
            # CPU busy fields are never resent (above), on this retry either.
            from hermes_cli.machine_cpu_metrics import FIELDS as CPU
            return send_compatible(send, {k: v for k, v in sample.items() if k not in COUNTERS and k not in CPU},
                                    on_counters_rejected, on_claiming_rejected)
        if not any(k in sample for k in ADDITIVE_FIELDS):
            raise
        return send({k: v for k, v in sample.items() if k not in ADDITIVE_FIELDS})
