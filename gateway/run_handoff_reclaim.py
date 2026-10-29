"""Handoff watcher: reclaim of handoffs stranded in ``running`` by a dead gateway, with retry.

``running`` is only set by the gateway watcher for one in-process dispatch, so a leftover row at
watcher startup belongs to a previous gateway that died mid-dispatch, and it blocks
``request_handoff`` for that session forever. The watcher reclaims each served profile store once
at startup. A reclaim that FAILS (busy state.db: the store returns ``None``) is not "nothing to do":
the store is kept and retried with backoff on later ticks (agent-9cbeb4e676).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("gateway.run")  # log-record parity with the watcher's origin module

RECLAIM_RETRY_MIN_S, RECLAIM_RETRY_MAX_S, RECLAIM_ERROR_AFTER = 30.0, 300.0, 3
reclaim_clock = time.monotonic  # module seam: tests drive the backoff with a fake clock


async def _reclaim_stale(runner: object) -> bool:
    """Fail handoffs left in ``running`` by a gateway that died mid-dispatch, in the currently scoped
    store. Defensive: a raising reclaim must not abort startup.

    Returns True when the store was reclaimed (even with nothing stranded) and False when the reclaim
    failed (``None`` from the store, or a raise), so the watcher retries it. A store without the method
    has nothing to reclaim: True."""
    reclaim = getattr(getattr(runner, "_session_db", None), "reclaim_stale_running_handoffs", None)
    if not callable(reclaim):
        return True
    try:
        ids = await reclaim(
            "gateway stopped mid-handoff; state reclaimed at startup. Re-run /handoff to try again.")
    except Exception:
        logger.debug("Stale-handoff reclaim raised", exc_info=True)
        return False
    if ids is None:
        return False
    if ids:
        logger.warning(
            "Reclaimed %d handoff(s) stranded in 'running' by a previous "
            "gateway: %s", len(ids), ", ".join(str(i) for i in ids))
    return True


class StaleHandoffReclaimer:
    """Per-watcher state: profile stores whose reclaim has not succeeded yet.

    ``reclaim_scope(profile_name, profile_home) -> bool`` runs one reclaim inside that store's scope.
    Successful startups keep "exactly once per store"; a failed store is retried at most every
    ``RECLAIM_RETRY_MIN_S`` doubling to ``RECLAIM_RETRY_MAX_S``, and after ``RECLAIM_ERROR_AFTER``
    failures ONE ERROR names the store (the signal detector files a card from it)."""

    def __init__(self, reclaim_scope: Callable[[Optional[str], Any], Awaitable[bool]]) -> None:
        self._reclaim_scope = reclaim_scope
        # profile name -> [profile_home, failures, next_attempt_at, error_logged]
        self.pending: dict[Optional[str], list] = {}

    async def startup(self, scopes) -> None:
        for name, home in scopes:
            if not await self._reclaim_scope(name, home):
                self._failed(name, self.pending.setdefault(name, [home, 0, 0.0, False]))

    async def retry_due(self, busy: bool) -> None:
        """Retry due stores. ``busy`` (this gateway has a dispatch in flight) skips the round: the
        reclaim fails EVERY 'running' row in the store, including our own live dispatch's."""
        if not self.pending or busy:
            return
        now = reclaim_clock()
        for name, entry in list(self.pending.items()):
            if now < entry[2]:
                continue
            if not await self._reclaim_scope(name, entry[0]):
                self._failed(name, entry)
                continue
            self.pending.pop(name, None)
            if entry[3]:
                logger.warning("Stale-handoff reclaim for profile store %s succeeded after %d failed "
                               "attempt(s)", name or "root", entry[1])

    def _failed(self, name: Optional[str], entry: list) -> None:
        entry[1] += 1
        entry[2] = reclaim_clock() + min(RECLAIM_RETRY_MIN_S * (2 ** (entry[1] - 1)), RECLAIM_RETRY_MAX_S)
        if entry[1] >= RECLAIM_ERROR_AFTER and not entry[3]:
            entry[3] = True
            logger.error(
                "Stale-handoff reclaim failed %d times for profile store %s; handoffs stranded in "
                "'running' there block /handoff for their sessions until a reclaim succeeds "
                "(still retrying every %.0fs)", entry[1], name or "root", RECLAIM_RETRY_MAX_S)
