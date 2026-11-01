"""Cross-turn stale-call circuit breaker for ``agent.chat_completion_helpers`` (re-exported there)."""

from __future__ import annotations

import contextlib
import logging
import time

logger = logging.getLogger("agent.chat_completion_helpers")


# ── Cross-turn stale-call circuit breaker (#58962) ─────────────────────
# A session wedged against an unresponsive provider would otherwise hit the
# stale detector on every call forever. ``agent._consecutive_stale_streams``
# is bumped on every stale kill and reset only when a call completes or the
# provider is swapped (switch_model / try_activate_fallback /
# restore_primary_runtime — the streak measured the OLD provider). Past the
# give-up threshold, calls abort immediately with an actionable error.

def _stale_streak(agent) -> int:
    try:
        return int(getattr(agent, "_consecutive_stale_streams", 0) or 0)
    except Exception:
        return 0


# [daveey] A stale kill measured across a host suspend is the sleep's, not the provider's. The stream
# watchdogs age on wall time, so the first check after a lid close (and every networkless DarkWake
# after it) kills the in-flight call; counting those gave up a healthy Kanban worker after five
# maintenance wakes (run 10714). ``time.monotonic`` stops while macOS/Linux sleep, so the gap between
# the two clocks grows by exactly the suspended time: a growth past this many seconds since the
# breaker last moved means the host slept in between.
_SUSPEND_SKEW_SECONDS = 30.0


def _wall_minus_awake() -> float:
    """Wall-clock seconds minus awake seconds: constant while awake, grows by each suspend."""
    return time.time() - time.monotonic()


def _mark_stale_clock(agent) -> None:
    with contextlib.suppress(Exception):
        agent._stale_streak_clock_skew = _wall_minus_awake()


def _slept_since_stale_mark(agent) -> bool:
    mark = getattr(agent, "_stale_streak_clock_skew", None)
    if not isinstance(mark, (int, float)):
        return False  # no baseline: no evidence of a suspend, the kill counts
    return _wall_minus_awake() - float(mark) > _SUSPEND_SKEW_SECONDS


def _bump_stale_streak(agent) -> None:
    """Count one stale kill toward the cross-turn breaker, unless the host suspended since the
    breaker last moved (that kill measured the sleep). Every call re-marks the clock gap, so the
    excuse covers only the kill right after a suspend, never later awake kills."""
    slept = _slept_since_stale_mark(agent)
    _mark_stale_clock(agent)
    if slept:
        logger.info("Stale kill after a host suspend not counted toward the stale breaker "
                    "(consecutive stale attempts stay %d).", _stale_streak(agent))
        return
    with contextlib.suppress(Exception):
        agent._consecutive_stale_streams = _stale_streak(agent) + 1


def _reset_stale_streak(agent) -> None:
    with contextlib.suppress(Exception):
        agent._consecutive_stale_streams = 0
    _mark_stale_clock(agent)


_INTERRUPTED_WAIT_STALE_SECONDS = 30.0


def _record_interrupted_provider_wait(agent, elapsed: float, *, response_started: bool) -> bool:
    """Count a user-aborted pre-response stall toward the stale breaker: past the
    wait-notice interval an interrupt is evidence of an unresponsive attempt.
    Mid-response and early interrupts stay neutral."""
    if response_started or elapsed < _INTERRUPTED_WAIT_STALE_SECONDS:
        return False
    _bump_stale_streak(agent)
    logger.warning("Interrupted provider wait counted as stale after %.0fs with no output; "
        "consecutive stale attempts=%d.", elapsed, _stale_streak(agent))
    return True
