"""Hold a job's fires while a provider's usage window is known to be closed (#89376).

A quota-exhausted provider answers with an explicit ``retry after <N>s`` (Codex 429: the
``AuthError`` from ``hermes_cli.auth_codex._codex_quota_exhausted_error``), and the routing
pool refuses before any request with ``SubscriptionQuotaUnavailable`` carrying ``resets_at``
(``agent.model_routing_unavailable.quota_wait_seconds`` maps every such type). When the whole
fallback chain is unavailable, re-firing on cadence is guaranteed to fail identically until
the window reopens — every fire is a usage probe plus a delivered failure alert. The failing
run's alert says the job is held; ``mark_job_run`` then parks ``next_run_at`` at the recovery
boundary (or the first legal occurrence after it, when several fall inside the window) and
stamps ``quota_hold_until`` so the stale-error re-arm
(``cron.jobs._job_is_stale_error_recurring``) does not pull the job back early.

Complement to ``cron/unreachable_retry.py``: this one moves ``next_run_at`` out of a known
closed provider window. Any run that reaches the model clears the marker.

The refusal's reset is an upper bound, not a fact: a pool reopens early when another OAuth
entry clears its cooldown. The hold therefore also stamps the refusing route
(``quota_hold_route``), and the due scan releases a held cron job to its next natural
occurrence as soon as that route's local credential pool is serving again
(``release_if_serving``; no network request, never an immediate catch-up fire). Without it a
hold prevented the very run that would have cleared it, so x-reply-discovery skipped a whole
day of slots while the same Codex pool served its sibling jobs.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, Optional

from cron import extra_fire
from hermes_time import now as _hermes_now, safe_strftime

logger = logging.getLogger("cron.scheduler")

# Persisted while a hold is active: ISO instant the job was parked at.
STATE_KEY = "quota_hold_until"
SCHEDULE_EXPR_KEY = "quota_hold_cron_expr"
# The route whose pool refused: {"provider": ..., "model": ...}; lets the due scan release early.
ROUTE_KEY = "quota_hold_route"
# Run-side flag (rides gateway.session_cron.RUN_JOB_FLAGS) carrying the route to mark_job_run.
ROUTE_FLAG = "_quota_hold_route"

# Holds planned before ROUTE_KEY existed: the failure the hold was planned from is persisted in
# the exact ``SubscriptionQuotaUnavailable`` message shape (optionally behind the RuntimeError
# wrapper's type prefix). Read only to RELEASE a hold, and only after the pool check agrees;
# text never parks a job.
_LEGACY_REFUSAL = re.compile(
    r"^(?:[A-Za-z_][\w.]*: )?Subscription quota unavailable for ([a-z0-9][a-z0-9_.-]*)/(\S+); "
    r"resets at [0-9.]+$")

# The provider's remaining seconds were measured when the probe ran; by the time the run is
# recorded a little wall clock has passed, so land clearly past the boundary.
HOLD_SLACK_SECONDS = 60

def hold_seconds_from_failure(exc: BaseException) -> Optional[float]:
    """Seconds the provider said it will stay closed, or None when *exc*'s cause chain does not
    end in a typed quota refusal with a deadline: the routing pool's ``SubscriptionQuotaUnavailable``
    (``resets_at``), a rate-limited ``AuthError`` (``retry after <N>s``), or a
    ``ProviderFallbackError`` whose every route is one of those. Anchored on the exception types,
    never on arbitrary text, so an unrelated "retry after" in an agent's output cannot park a job.
    The type list lives in ``agent.model_routing_unavailable.quota_wait_seconds`` (one mapping for
    every consumer)."""
    from agent.model_routing_unavailable import quota_wait_seconds

    return quota_wait_seconds(exc)


def classify_failure_hold(job: dict[str, Any], exc: BaseException) -> Optional[float]:
    """The hold for a failed run, or None. A quota-class failure that maps to no hold (no usable
    deadline, or a new refusal type the mapping misses) logs ONE "quota failure not held" warning
    per failed run: the self-heal lane keys on that line instead of every hourly traceback."""
    from agent.model_routing_unavailable import is_quota_failure

    hold = hold_seconds_from_failure(exc)
    if hold is None and is_quota_failure(exc):
        logger.warning(
            "Job '%s': quota failure not held (no usable reset deadline in %s: %s); it will re-fire "
            "on cadence", job.get("name", job.get("id", "?")), type(exc).__name__, exc)
    return hold


def hold_active(job: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True while the job is parked inside a provider window (an expired marker is inert)."""
    from cron.jobs import _instant_after, _parse_aware  # late: jobs imports this module's helpers

    until = _parse_aware(job.get(STATE_KEY)) if job.get(STATE_KEY) else None
    return until is not None and _instant_after(until, now or _hermes_now())


def clear_state(job: dict[str, Any]) -> None:
    job.pop(STATE_KEY, None)
    job.pop(SCHEDULE_EXPR_KEY, None)
    job.pop(ROUTE_KEY, None)


def route_from_failure(exc: BaseException) -> Optional[dict[str, str]]:
    """The refusing route to stamp on the hold, or None when the refusal does not name one."""
    from agent.model_routing_unavailable import quota_refusal_route

    route = quota_refusal_route(exc)
    return None if route is None else {"provider": route[0], "model": route[1]}


def _valid_route(route: Any) -> Optional[dict[str, str]]:
    if not isinstance(route, dict):
        return None
    provider, model = route.get("provider"), route.get("model")
    if isinstance(provider, str) and provider and isinstance(model, str) and model:
        return {"provider": provider, "model": model}
    return None


def hold_route(job: dict[str, Any]) -> Optional[dict[str, str]]:
    """The route an active hold waits on: the stamped ``quota_hold_route``, else (a hold planned
    before the stamp existed) the route named by the persisted pool refusal it was planned from.
    None keeps the hold one-way until its timestamp, as before."""
    stamped = _valid_route(job.get(ROUTE_KEY))
    if stamped is not None:
        return stamped
    m = _LEGACY_REFUSAL.match(str(job.get("last_error") or "").strip())
    return {"provider": m.group(1), "model": m.group(2)} if m else None


def pool_serving(route: dict[str, str]) -> bool:
    """True when the route's local OAuth credential pool can serve its model right now.

    The same local evidence the router refuses on (``quota_unavailable``: a known cooldown on
    every live OAuth entry), plus positive availability so an empty pool never counts as serving.
    Reads the credential store only; no provider request. Any read fault keeps the hold."""
    try:
        from agent.credential_pool import load_pool
        from agent.model_routing_unavailable import quota_unavailable

        pool = load_pool(route["provider"], allowed_auth_types={"oauth"})
        return quota_unavailable(pool, route["model"]) is None and bool(
            pool.has_available(model=route["model"]))
    except Exception as exc:  # health: allow BLE001 -- a pool read fault must keep the hold, never break the due scan
        logger.debug("quota hold: pool check failed for %s/%s (%s)",
                     route.get("provider"), route.get("model"), exc, exc_info=True)
        return False


def release_if_serving(
    job: dict[str, Any], now: datetime, natural_next: Optional[str],
) -> bool:
    """Release a held cron job whose refusing route is serving again (due scan, jobs lock held).

    *natural_next* is the schedule's next occurrence after *now*. On release the marker is
    cleared and ``next_run_at`` moves to that occurrence: never "now", so a recovered pool gets
    no catch-up fire. Only cron holds, earlier than the parked instant, with a known route and
    no queued run-now (that run-now waits for the boundary, ``extra_fire.reschedule``).
    Interval holds stay one-way: a cleared marker on an errored interval job lets the stale-error
    re-arm fire it immediately. Returns True when released (the caller persists the record)."""
    from cron.jobs import _instant_before, _parse_aware

    if (job.get("schedule") or {}).get("kind") != "cron" or not hold_active(job, now):
        return False
    if extra_fire.queued(job) or not natural_next:
        return False
    parked, natural = _parse_aware(job.get("next_run_at")), _parse_aware(natural_next)
    if parked is None or natural is None or not _instant_before(natural, parked):
        return False
    route = hold_route(job)
    if route is None or not pool_serving(route):
        return False
    clear_state(job)
    job["next_run_at"] = natural_next
    logger.info(
        "Job '%s': quota hold released early: pool serving (%s/%s); next run %s instead of %s",
        job.get("name", job.get("id", "?")), route["provider"], route["model"],
        natural_next, parked.isoformat())
    return True


def is_recovery_fire(job: dict[str, Any], next_run: str) -> bool:
    """True for the exact off-lattice cron fire parked by ``plan_hold``.

    The expression fingerprint keeps a direct ``jobs.json`` schedule edit from inheriting the
    exception: edited schedules must still re-anchor without firing.
    """
    schedule = job.get("schedule") or {}
    return (
        schedule.get("kind") == "cron"
        and job.get(STATE_KEY) == next_run
        and job.get(SCHEDULE_EXPR_KEY) == schedule.get("expr")
    )


def _window_end(hold_seconds: float) -> datetime:
    from cron.jobs import _seconds_after

    return _seconds_after(_hermes_now(), float(hold_seconds) + HOLD_SLACK_SECONDS)


def _recovery_worthwhile(
    job: dict[str, Any], natural_next: datetime, window_end: datetime,
) -> bool:
    """One off-lattice recovery fire, and only for a sparse schedule.

    Bounded: a job already carrying ``quota_hold_until`` IS the recovery fire failing again, so
    it waits for the natural schedule instead of re-parking at every hold boundary (the
    probe-per-window cost the hold exists to prevent). Sparse: the natural occurrence must be at
    least half a cadence period past the boundary — the same half-period rule as
    ``cron.jobs._compute_grace_seconds`` — otherwise the recovery fire is a near-duplicate of the
    natural one (hourly job, hold ending at :58, would fire :58 AND :00).
    """
    from cron.jobs import _elapsed_seconds, _schedule_cadence_seconds

    if job.get(STATE_KEY):
        return False
    cadence = _schedule_cadence_seconds(job.get("schedule") or {})
    return bool(cadence) and _elapsed_seconds(natural_next, window_end) >= cadence / 2


def plan_hold(
    job: dict[str, Any], hold_seconds: float, *, recover_consumed_fire: bool = False,
    route: Optional[dict[str, str]] = None,
) -> bool:
    """Called under the jobs lock AFTER ``_advance_after_run`` computed the schedule's natural
    ``next_run_at`` for a failed run. A scheduled sparse cron may retry its consumed fire at the
    recovery boundary; manual runs keep the natural schedule. Otherwise coalesce fires through
    the closed window. *route* (``route_from_failure``) is stamped so the due scan can release
    the hold early when that pool serves again. Returns True when parked."""
    from cron.jobs import _instant_before, _parse_aware, compute_next_run

    schedule = job.get("schedule") or {}
    kind = schedule.get("kind")
    if kind not in {"cron", "interval"} or job.get("state") == "paused":
        clear_state(job)
        return False
    window_end = _window_end(hold_seconds)
    natural_next = _parse_aware(job.get("next_run_at"))
    blocked = natural_next is None or _instant_before(natural_next, window_end)
    recover = (kind == "cron" and not blocked and recover_consumed_fire
               and _recovery_worthwhile(job, natural_next, window_end))
    if not blocked and not recover:
        clear_state(job)
        return False
    if kind == "cron" and blocked and not extra_fire.queued(job):
        # Coalesce cron occurrences inside the closed window to the first legal instant after it.
        # A queued run-now is not an occurrence: it waits for the boundary, not the next slot.
        parked = compute_next_run(
            schedule, window_end.isoformat(), timezone=job.get("timezone")
        ) or window_end.isoformat()
    else:
        parked = window_end.isoformat()
    if recover:
        # Only the recovery fire is off-lattice; the coalesced instant is a legal occurrence.
        job[SCHEDULE_EXPR_KEY] = schedule.get("expr")
    else:
        job.pop(SCHEDULE_EXPR_KEY, None)
    extra_fire.reschedule(job, parked)
    job[STATE_KEY] = parked
    stamped = _valid_route(route)
    if stamped is not None:
        job[ROUTE_KEY] = stamped
    else:
        job.pop(ROUTE_KEY, None)
    logger.warning(
        "Job '%s': provider usage window closed for %.0fs — holding fires until %s instead of "
        "failing on every cadence tick",
        job.get("name", job.get("id", "?")), float(hold_seconds), parked)
    return True


def hold_notice(job: dict[str, Any], hold_seconds: Optional[float]) -> str:
    """Line appended to the ONE failure alert delivered on entering the hold, else ""."""
    if not hold_seconds or (job.get("schedule") or {}).get("kind") not in {"cron", "interval"}:
        return ""
    window_end = _window_end(hold_seconds)
    hours = float(hold_seconds) / 3600.0
    return (
        f"\nThe provider's usage window is closed for about {hours:.1f}h. This job is held "
        f"through {safe_strftime(window_end, '%Y-%m-%d %H:%M %Z')} and resumes at the first safe "
        "opportunity afterwards; no further alerts are sent while the provider is unavailable."
    )
