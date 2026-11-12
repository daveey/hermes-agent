"""Host-side waits on a compression worker (sibling of ``conversation_compression``): the progress-aware
pre-commit wait (idle budget + ceiling, plus the core-update yield of t_01a1d0aa52ee1404) and the bounded wait on
an admitted commit. ``conversation_compression`` re-exports these names and its host loop calls them through its
own module globals, so tests keep patching them there.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from agent.conversation_compression import CompressionCommitFence

# Same logger name as the origin module so log records / caplog filters are unchanged.
logger = logging.getLogger("agent.conversation_compression")

_COMMIT_OVERRUN_WAIT_SLICE_SECONDS = 30.0
# [daveey] t_01a1d0aa52ee1404: the host wait's answer when ``yield_check`` asked it to stop (a core-update pause).
YIELDED = object()
_YIELD_POLL_S = 0.25


def _yield_requested(yield_check: Callable[[], bool]) -> bool:
    try:
        return bool(yield_check())
    except Exception:
        logger.debug("compression yield check failed", exc_info=True)
        return False


def _await_worker_within_budget(
    future: Any, fence: CompressionCommitFence, *, idle: float, ceiling: float, wait_started: float,
    yield_check: Optional[Callable[[], bool]] = None,
) -> tuple[bool, Any]:
    """Poll ``future`` under the idle budget + ceiling; ``(True, result)`` when it settled, ``(False, YIELDED)``
    when ``yield_check`` asked the host to stop waiting (polled every ``_YIELD_POLL_S``)."""
    while True:
        # The fence deadline moves: prompt reading on llama-server pushes it back.
        remaining_ceiling = (fence.deadline_monotonic or wait_started + ceiling) - time.monotonic()
        if remaining_ceiling <= 0:
            return False, None
        # Charge idle budget from LAST PROGRESS, not slice start, or silence could approach 2x the budget.
        since_progress = fence.seconds_since_progress()
        wait_slice = min(max(idle - since_progress, 0.005), remaining_ceiling)
        if yield_check is not None:
            wait_slice = min(wait_slice, _YIELD_POLL_S)
        try:
            return True, future.result(timeout=wait_slice)
        except concurrent.futures.TimeoutError:
            # Aliases builtin TimeoutError (3.11+): also fires when the WORKER died with one (#63892).
            # A settled future never unsettles — re-waiting spun ~2k iter/s; take the stall path now.
            if future.done():
                exc = future.exception()
                if exc is None:
                    return True, future.result()
                logger.info("Context compression worker exited with %r — taking the stall path", exc)
                return False, None
            if yield_check is not None and _yield_requested(yield_check):
                return False, YIELDED
            waited = time.monotonic() - wait_started
            since_progress = fence.seconds_since_progress()
            if not fence.deadline_exceeded and since_progress < idle:
                # Logged once per idle window, not per yield poll slice.
                if yield_check is None or waited // idle != (waited - wait_slice) // idle:
                    logger.info(
                        "Context compression still streaming after %.0fs (last progress %.1fs ago) — extending "
                        "wait (ceiling %.0fs)", waited, since_progress, ceiling,
                    )
                continue
            return False, None


def _await_in_flight_commit(
    future: Any, *, ceiling: float, wait_started: float, on_commit_overrun: Optional[Callable[[float, float], None]]
) -> Any:
    """begin_commit won the race: the SessionDB mutation cannot be fence-cancelled, so wait
    in bounded slices, logging (escalating) + surfacing once via ``on_commit_overrun``
    WHILE the commit hangs. Never silently hung or abandoned.
    """
    overrun_surfaced = False
    overrun_reports = 0
    while True:
        waited = time.monotonic() - wait_started
        remaining = ceiling - waited
        if remaining <= 0:
            # Bounded increments so each overrun window is visible in logs rather than one silent unbounded block.
            remaining = min(_COMMIT_OVERRUN_WAIT_SLICE_SECONDS, max(ceiling, 0.05))
            overrun_reports += 1
            log = logger.warning if overrun_reports <= 2 else logger.error
            log(
                "Context compression SessionDB commit still running "
                "%.1fs past the total ceiling (waited %.1fs, ceiling %.1fs); commit cannot be abandoned mid-flight — "
                "continuing to wait (check SessionDB health if this persists)", waited - ceiling, waited,
                ceiling,
            )
            if not overrun_surfaced and on_commit_overrun is not None:
                overrun_surfaced = True
                try:
                    on_commit_overrun(waited, ceiling)
                except Exception:
                    logger.debug("compress_context commit-overrun callback failed", exc_info=True)
        try:
            return future.result(timeout=remaining)
        except concurrent.futures.TimeoutError:
            # Aliases builtin TimeoutError (3.11+): also fires when the commit worker died with one (#63892).
            # A settled future never unsettles — this ceiling-less loop spun forever; re-raise the worker's error.
            if future.done():
                return future.result()
            # Commit-phase progress is informative only — the commit must complete; loop
            # and re-report with the updated overrun window.
            continue
