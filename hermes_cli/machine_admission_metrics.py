"""Versioned hub pool telemetry; operator policy is never inferred by the receiver."""
from pathlib import Path

FIELDS = ("admission_version", "admission_regular_slots", "admission_priority_slots",
          "admission_regular_cpu_limit", "admission_priority_cpu_limit", "active_regular_runs", "active_priority_runs")


def policy():
    """Read one root configuration snapshot, returning no support for invalid/disabled policy."""
    from hermes_constants import get_default_hermes_root
    from hermes_cli.config_backend import read_config_doc_readonly
    from hermes_cli import machine_cpu_metrics as cpu
    from hermes_cli.kanban_db_dispatch import resolve_max_in_progress

    try:
        doc = read_config_doc_readonly(Path(get_default_hermes_root()) / "config.yaml")
        state, limit = cpu.root_limit(read_doc=lambda _path: doc)
        if state != cpu.ON:
            return {}
        kanban = doc["kanban"]
        admission = kanban["admission"]
        slots = admission.get("priority_slots", 0)
        ceiling = admission.get("priority_cpu_busy_below_pct", 85)
        raw = kanban.get("max_in_progress")
        if type(slots) is not int or not 0 < slots <= 256 or not cpu.valid_limit(ceiling):
            return {}
        if raw is not None and (type(raw) is not int or raw < 1):
            return {}
        cap = resolve_max_in_progress(raw)
        if type(cap) is not int or not 0 <= cap <= 256:
            return {}
        return {"admission_version": 1, "admission_regular_slots": cap, "admission_priority_slots": slots,
                "admission_regular_cpu_limit": limit, "admission_priority_cpu_limit": ceiling}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def collect(count):
    """Use a single census for the scalar and both pools, or omit the entire tuple."""
    from hermes_cli.kanban_sync_cpu import detail as diagnostic
    settings = diagnostic("collect.policy", policy)
    if not settings:
        return {"active_card_runs": diagnostic("collect.census", count)}
    detail = {"pools": {"regular": 0, "priority": 0}}
    active = diagnostic("collect.census", lambda: count(detail=detail))
    out = {"active_card_runs": active}
    pools = detail["pools"]
    if type(active) is int and active >= 0 and all(type(v) is int and v >= 0 for v in pools.values()) and sum(pools.values()) == active:
        out.update(settings, active_regular_runs=pools["regular"], active_priority_runs=pools["priority"])
    return out
