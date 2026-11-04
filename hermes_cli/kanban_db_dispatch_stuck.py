"""The "dispatcher stuck" warning and the tick checks deciding whether a zero-spawn tick counts. [daveey]

Moved out of ``kanban_db_dispatch`` (which re-exports these names). ``results`` are ``DispatchResult`` rows.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from hermes_cli.kanban_db_dispatch_clock import all_clock_held, deadline_suffixes
from hermes_cli.kanban_repin_notice import MANUAL_REPIN

__all__ = ["all_at_capacity", "all_clock_held", "cap_source", "describe_suppression", "manual_repin_ids",
           "stuck_warning"]


def manual_repin_ids(results: Iterable[Optional[Any]]) -> list[str]:
    """Ready cards no pool worker can run, which wait for their creator's repin (``kanban_repin_notice``)."""
    return sorted({tid for res in results if res is not None
                   for tid, reason in res.skipped_ineligible if reason == MANUAL_REPIN})


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
            if reason == MANUAL_REPIN:
                continue  # named on its own (manual_repin_ids): not a hold this machine's lease explains
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
    except Exception:  # health: allow BLE001 -- cap source is decoration on a warning line; never break the warning
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
    repin = manual_repin_ids(results)
    msg = (f"kanban dispatcher stuck: ready queue non-empty for {bad_ticks} consecutive ticks but 0 workers "
           f"spawned.{f' Last tick held back: {held}.' if held else ''}")
    if repin:
        # No pool worker can run these (Board ``no_eligible_worker``); each card carries the repin command.
        msg += f" Not a stall, waiting for a manual repin: {', '.join(repin)}."
    lease_bound = any(res is not None and (res.claim_deferred or any(
        reason != MANUAL_REPIN for _tid, reason in res.skipped_ineligible)) for res in results)
    if lease_bound:
        # With sync enabled every claim goes through the Board lease: a stopped sync loop explains them all. [daveey]
        from hermes_cli import kanban_sync_runstate as runstate
        stopped = runstate.describe()
        if stopped:
            return (msg + f" The Board lease refused or deferred claims and {stopped}. Restart the gateway once "
                    "the cause is fixed (`hermes kanban sync status` shows the same state).")
        return (msg + " The Board lease refused or deferred claims: check the cards' runs_on / placement "
                "(`hermes kanban show <id>`) and which machine holds or may take their Board lease.")
    return msg + " Check profile health (venv, PATH, credentials) and `hermes kanban list --status ready`."


def all_at_capacity(results: Iterable[Optional[Any]]) -> bool:
    """True when every board's tick that ran was held only by a full concurrency cap (nothing could spawn).

    The "dispatcher stuck" detectors skip such ticks: ready work waiting behind ``kanban.max_in_progress``
    running workers is the cap doing its job (stale/timeout reclaim frees hung workers). [daveey]"""
    ran = [res for res in results if res is not None and not res.skipped_locked and not res.skipped_owned]
    return bool(ran) and all(res.at_capacity for res in ran)
