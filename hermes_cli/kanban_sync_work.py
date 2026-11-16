"""Feed-keyed invalidation, disconnected polling, failed-round backoff and independent full recovery."""
from __future__ import annotations

import threading
from pathlib import Path
from collections.abc import Callable
from typing import Optional

# A board whose round failed is not rerun for its pending outbox, a dirty flag or the poll until this backoff
# passes: 1 s, doubling, capped at the feed socket's own 60 s reconnect cap. Incident (2026-11-15, David-MBP,
# t_01a1e1174026210f): DNS failed for 25 min and pending local edits reselected every board on each 0.5 s wait
# tick, so the loop retried the failing rounds about every 0.6 s (the socket's backoff never covered rounds). The
# outbox rows stay pending (push keeps them on a transport failure); a success, or the board's feed socket
# reconnecting (the Board is reachable again), clears the backoff. [daveey]
FAIL_BACKOFF_MIN_S = 1.0
FAIL_BACKOFF_MAX_S = 60.0


class SyncWork:
    def __init__(self, period: float) -> None:
        self.period = period
        self.deadline = 0.0
        self._recovery_deadline = 0.0
        self._connected: set[str] = set()
        self._failed: set[str] = set()
        self._lock = threading.Lock()
        self._dirty: set[str] = set()
        self._known: set[tuple[str, Path]] = set()
        self._streak: dict[str, int] = {}  # board -> consecutive failed rounds
        self._retry_at: dict[str, float] = {}  # board -> monotonic end of its failed-round backoff

    def changed(self, board: str) -> None:
        with self._lock:
            self._dirty.add(board)

    def feed_state(self, board: str, connected: bool) -> None:
        with self._lock:
            if connected:
                self._connected.add(board)
                self._retry_at.pop(board, None)  # the Board answered the socket: retry the round now
            else:
                self._connected.discard(board)

    def finished(self, board: str, success: bool, now: Optional[float] = None) -> None:
        """Record a round's outcome. A failure with ``now`` (monotonic) starts or doubles the board's backoff."""
        # Only the sync thread owns this set; feed callbacks never clear retries.
        if success:
            self._failed.discard(board)
            with self._lock:
                self._streak.pop(board, None)
                self._retry_at.pop(board, None)
            return
        self._failed.add(board)
        if now is None:
            return
        with self._lock:
            streak = self._streak[board] = self._streak.get(board, 0) + 1
            self._retry_at[board] = now + min(FAIL_BACKOFF_MIN_S * 2 ** min(streak - 1, 16), FAIL_BACKOFF_MAX_S)

    def backing_off(self, board: str, now: float) -> bool:
        with self._lock:
            return now < self._retry_at.get(board, 0.0)

    def next_retry(self, now: float) -> Optional[float]:
        """Seconds until the earliest backing-off board may run again (None: no board is backing off)."""
        with self._lock:
            due = [at for at in self._retry_at.values() if at > now]
        return min(due) - now if due else None

    def select(self, boards: list[tuple[str, Path]], now: float,
               pending: Callable[[list[Path]], bool]) -> list[tuple[str, Path]]:
        # Consume before starting IO: an event arriving during that IO remains dirty
        # for the next round, even if its event notification is coalesced.
        with self._lock:
            dirty, self._dirty = self._dirty, set()
            connected = self._connected.copy()
            waiting = {slug for slug, at in self._retry_at.items() if now < at}
            self._dirty |= dirty & waiting  # a backing-off board keeps its flag for the round after the backoff
        current = set(boards)
        new = current - self._known
        self._known = current
        poll = now >= self.deadline
        if poll:
            self.deadline = now + self.period
        if now >= self._recovery_deadline:
            # Connected boards invalidate through feed frames, not a timer-only TTL.
            # Retain an independent safety audit for an undetected missed frame.
            self._recovery_deadline = now + max(60.0, self.period)
            return [(slug, path) for slug, path in boards if slug not in waiting]
        return [(slug, path) for slug, path in boards
                if slug not in waiting and ((slug, path) in new or slug in dirty
                                            or (poll and (slug not in connected or slug in self._failed))
                                            or pending([path]))]
