"""Respawn-guard holds that end on a known clock, for the "dispatcher stuck" warnings. [daveey]

hermes-hub 2026-11-01: 43 ready cards waited on a Codex quota wall with a typed reset deadline, and the
dispatcher logged "stuck" every 5 minutes with a bare ``subscription_quota_reset=43``. A card held until a
known deadline is waiting, not stuck: the warning names the deadline, and a tick whose every ready card waits
on a clock is not a stuck tick (:func:`all_clock_held`).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Guard reasons whose hold ends at a deadline the dispatcher can compute from the card's own runs.
CLOCK_REASONS = frozenset({
    "subscription_quota_reset",  # typed reset in the latest attempt's receipt
    "rate_limit_cooldown",       # latest rate_limited run + rate-limit cooldown
    "infrastructure_cooldown",   # latest refused spawn + the same cooldown
    "owner_restart_cooldown",    # run lost to its owner's restart + owner-restart cooldown
})
# The reasons ``kanban_quota_episode.decision`` holds an episode card on.
_EPISODE_REASONS = frozenset({"subscription_quota_reset", "rate_limit_cooldown"})


def guard_hold_until(conn: sqlite3.Connection, task_id: str, reason: str, now: int) -> Optional[int]:
    """Epoch at which ``reason``'s hold on ``task_id`` ends, or None (no known clock, or already over)."""
    if reason not in CLOCK_REASONS:
        return None
    from hermes_cli import kanban_db as kb
    if reason in _EPISODE_REASONS:
        # A quota episode's deadline (doubling backoff, every provider's wall) is what the guard holds on, or the
        # earlier reset that frees a fallback route (kanban_quota_episode.release_at).
        from hermes_cli.kanban_quota_episode import episode, release_at
        task = kb.get_task(conn, task_id)
        until = release_at(conn, task, now) if task is not None else None
        if until is not None:
            return int(until) if until > now else None
        from hermes_cli.kanban_quota_episode_release import covers_latest
        if covers_latest(conn, task_id) or episode(conn, task_id):  # released, expired or stale: no clock
            return None
    if reason == "subscription_quota_reset":
        reset = _receipt_reset_at(conn, task_id)
        return int(reset) if reset is not None and reset > now else None
    row = conn.execute(
        "SELECT ended_at FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC LIMIT 1", (task_id,)).fetchone()
    ended_at = kb._to_epoch(row[0]) if row is not None else None
    if ended_at is None:
        return None
    if reason == "owner_restart_cooldown":
        from hermes_cli.kanban_db_dispatch import OWNER_RESTART_COOLDOWN_SECONDS
        cooldown = kb._env_int("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", OWNER_RESTART_COOLDOWN_SECONDS)
    else:
        cooldown = kb._resolve_rate_limit_cooldown_seconds()
    until = int(ended_at) + int(cooldown)
    return until if until > now else None


def record_clock_hold(conn: sqlite3.Connection, result: Any, task_id: str, reason: str) -> None:
    """Record in ``result.clock_held`` when ``reason``'s hold on ``task_id`` ends, if it ends on a known clock.

    Best effort: a failed lookup only drops the deadline from the warning, it never stops the tick."""
    try:
        until = guard_hold_until(conn, task_id, reason, int(time.time()))
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        logger.debug("kanban: guard deadline lookup failed for %s", task_id, exc_info=True)
        return
    if until is not None:
        result.clock_held[task_id] = (reason, until)


def _receipt_reset_at(conn: sqlite3.Connection, task_id: str) -> Optional[float]:
    """The typed reset in the latest ended run's ``rate_limited`` receipt (the record
    ``kanban_quota_receipt.waiting_for_quota_reset`` admits on), or None."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_quota_receipt import quota_fields
    row = conn.execute(
        "SELECT e.payload FROM task_runs r JOIN task_events e ON e.run_id=r.id "
        "AND e.task_id=r.task_id AND e.kind='worker_result' "
        "WHERE r.id=(SELECT id FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC,id DESC LIMIT 1) AND r.outcome='rate_limited' "
        "ORDER BY e.id DESC LIMIT 1", (task_id,)).fetchone()
    if row is None:
        return None
    receipt = kb._json_dict(row[0])
    if receipt.get("exit_code") != kb.KANBAN_RATE_LIMIT_EXIT_CODE:
        return None
    return quota_fields(receipt).get("failure_resets_at")


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def deadline_suffixes(results: Iterable[Any]) -> dict[str, str]:
    """``reason -> " (until <earliest>[..<latest>])"`` over the clock-held cards of ``results``."""
    spans: dict[str, tuple[int, int]] = {}
    for res in results:
        if res is None:
            continue
        for _task_id, (reason, until) in res.clock_held.items():
            lo, hi = spans.get(reason, (until, until))
            spans[reason] = (min(lo, until), max(hi, until))
    return {reason: f" (until {_iso(lo)}{'' if lo == hi else '..' + _iso(hi)})"
            for reason, (lo, hi) in spans.items()}


def all_clock_held(results: Iterable[Any]) -> bool:
    """True when every board that ticked spawned nothing and every lane candidate it saw waits on a known clock.

    Boards skipped for a lock or another dispatch owner say nothing either way. At least one board must hold
    a card on a clock: a tick that saw no candidates at all is not evidence the queue is waiting."""
    ran = [res for res in results if res is not None and not res.skipped_locked and not res.skipped_owned]
    if not ran or not any(res.clock_held for res in ran):
        return False
    # Pacing also records refresh deadlines here; only its typed verdict can prove a real hold.
    # The expected-hold monitor handles pacing separately, including bounded starvation diagnostics.
    return all(not res.spawned and not res.llm_paced and len(res.clock_held) == res.lane_candidates for res in ran)
