"""The "dispatcher stuck" warning and the tick checks deciding whether a zero-spawn tick counts. [daveey]

Moved out of ``kanban_db_dispatch`` (which re-exports these names). ``results`` are ``DispatchResult`` rows.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from hermes_cli.kanban_db_dispatch_clock import all_clock_held, deadline_suffixes

__all__ = ["all_at_capacity", "all_clock_held", "cap_source", "describe_suppression", "stuck_warning"]


def describe_suppression(results: Iterable[Optional[Any]]) -> str:
    """One line naming why the tick(s) held ready work back, or ``""``.

    ``active_pr=1, recent_success=2, rate_limited=1, skipped_locked=1,
    memory_pressure=critical`` — the respawn-guard reasons counted per task
    plus the tick-level holds. Feeds the "dispatcher stuck" warnings of the
    CLI daemon and the embedded gateway dispatcher, which otherwise report a
    bare zero-spawn count while ``hermes kanban tail`` is the only place the
    guard reason is written (#111910).
    """
    results = list(results)
    counts: dict[str, int] = {}
    pressure: Optional[str] = None
    capacity: Optional[str] = None
    for res in results:
        if res is None:
            continue
        for _task_id, reason in res.respawn_guarded:
            counts[reason] = counts.get(reason, 0) + 1
        if res.rate_limited:
            counts["rate_limited"] = counts.get("rate_limited", 0) + len(res.rate_limited)
        if res.skipped_locked:
            counts["skipped_locked"] = counts.get("skipped_locked", 0) + 1
        if res.skipped_owned:
            counts["skipped_owned"] = counts.get("skipped_owned", 0) + 1
        for _task_id, reason in res.claim_deferred:
            key = f"claim_deferred({reason})"
            counts[key] = counts.get(key, 0) + 1
        for _task_id, reason in res.skipped_ineligible:
            key = f"skipped_ineligible({reason})"
            counts[key] = counts.get(key, 0) + 1
        if res.memory_pressure:
            pressure = res.memory_pressure
        if res.at_capacity:
            capacity = res.at_capacity
    until = deadline_suffixes(results)
    parts = [f"{k}={v}{until.get(k, '')}" for k, v in sorted(counts.items())]
    if capacity:
        parts.append(f"at_capacity({capacity})")
        source = cap_source(capacity)
        if source:
            parts.append(f"cap_source={source}")
    if pressure:
        parts.append(f"memory_pressure={pressure}")
    return ", ".join(parts)


def cap_source(capacity: str) -> str:
    """Where the cap named in an ``at_capacity`` string came from, with the config file it was read from.

    A profile gateway that holds the host dispatcher lock applies ITS config's caps to every board; on
    hermes-hub one ran a memory-derived ``max_in_progress=8`` instead of the root's 16 for hours and the
    warning never said whose cap it was. [daveey]
    """
    try:
        from hermes_constants import get_hermes_home
        config = f"{get_hermes_home()}/config.yaml"
    except Exception:
        config = "config.yaml"
    if capacity.startswith("max_in_progress="):
        from hermes_cli.kanban_db_dispatch import configured_max_in_progress
        if configured_max_in_progress() is not None:
            return f"kanban.max_in_progress in {config}"
        return f"memory-derived default (kanban.max_in_progress unset in {config})"
    if capacity.startswith("max_spawn="):
        return f"kanban.max_spawn in {config} or the --max flag"
    return ""


def stuck_warning(bad_ticks: int, results: Iterable[Optional[Any]]) -> str:
    """The "dispatcher stuck" line for the CLI daemon and the embedded gateway dispatcher.

    When the Board deferred claims or the queue held cards this machine cannot lease, the line says so and points
    at the Board leases and ``runs_on``; it only blames profile health when nothing else explains the zero."""
    results = list(results)
    held = describe_suppression(results)
    msg = (f"kanban dispatcher stuck: ready queue non-empty for {bad_ticks} consecutive ticks but 0 workers "
           f"spawned.{f' Last tick held back: {held}.' if held else ''}")
    lease_bound = any(res is not None and (res.claim_deferred or res.skipped_ineligible) for res in results)
    if lease_bound:
        return (msg + " The Board lease refused or deferred claims: check the cards' runs_on / placement "
                "(`hermes kanban show <id>`) and which machine holds or may take their Board lease.")
    return msg + " Check profile health (venv, PATH, credentials) and `hermes kanban list --status ready`."


def all_at_capacity(results: Iterable[Optional[Any]]) -> bool:
    """True when every board's tick that ran was held only by a full concurrency cap (nothing could spawn).

    The "dispatcher stuck" detectors skip such ticks: ready work waiting behind ``kanban.max_in_progress``
    running workers is the cap doing its job (stale/timeout reclaim frees hung workers). [daveey]"""
    ran = [res for res in results if res is not None and not res.skipped_locked and not res.skipped_owned]
    return bool(ran) and all(res.at_capacity for res in ran)
