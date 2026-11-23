"""Single-flight tickets and freshness for the linked-cards projection (``kanban_session_activity``). [daveey]

Every strict reader takes an arrival ticket before its fingerprint; every projection takes a start ticket before it
reads any store. A cached projection whose start ticket is later than a reader's arrival read every store after that
reader's own writes, so it answers the reader (read-your-writes) even though the fingerprint moved meanwhile.

Without this, on a board written every few seconds each reader queued on ``_links_lock`` rebuilt in turn: hub
2026-11-22, 412 strict projections in 57 min at ~6 s wall each, the lock held ~98% of the time and Desktop user-steps
polls timing out behind it while the hub sat idle. ``publish``/``answers``/``reset`` run under ``_links_lock``."""
import itertools
import threading
from typing import Callable

from hermes_cli import kanban_links_narrow as _links_narrow


class StartTickets:
    def __init__(self, clock: Callable[[], float]):
        self._clock = clock  # the owner's monotonic clock, looked up per call (tests replace it)
        self._tickets = itertools.count(1)
        self._take_lock = threading.Lock()  # next() on count is atomic only under the GIL
        self._started: dict[tuple, int] = {}

    def take(self) -> int:
        """A reader's arrival or a projection's start; strictly increasing across threads."""
        with self._take_lock:
            return next(self._tickets)

    def publish(self, scope: tuple, started: int, live_scopes) -> None:
        """``scope``'s cached projection is now the one that took ``started``; forget scopes the cache evicted."""
        self._started[scope] = started
        for stale in [key for key in self._started if key not in live_scopes]:
            del self._started[stale]

    def answers(self, scope: tuple, arrival: int, hit: tuple) -> bool:
        """``hit`` (the cached entry) started after ``arrival`` and is within its max age."""
        return self._started.get(scope, 0) > arrival and self._clock() < hit[0]

    def reset(self) -> None:
        self._started.clear()

    def fresh(self, hit: tuple, fingerprint: tuple) -> bool:
        """Within its max age, and read under ``fingerprint`` except for boards it never read (``kanban_links_narrow``)."""
        return self._clock() < hit[0] and (hit[2] == fingerprint or _links_narrow.matches(hit, fingerprint))
