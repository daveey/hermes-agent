"""Feed-keyed invalidation, disconnected polling and independent full recovery."""
from __future__ import annotations

import threading
from pathlib import Path
from collections.abc import Callable


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

    def changed(self, board: str) -> None:
        with self._lock:
            self._dirty.add(board)

    def feed_state(self, board: str, connected: bool) -> None:
        with self._lock:
            if connected:
                self._connected.add(board)
            else:
                self._connected.discard(board)

    def finished(self, board: str, success: bool) -> None:
        # Only the sync thread owns this set; feed callbacks never clear retries.
        if success:
            self._failed.discard(board)
        else:
            self._failed.add(board)

    def select(self, boards: list[tuple[str, Path]], now: float,
               pending: Callable[[list[Path]], bool]) -> list[tuple[str, Path]]:
        # Consume before starting IO: an event arriving during that IO remains dirty
        # for the next round, even if its event notification is coalesced.
        with self._lock:
            dirty, self._dirty = self._dirty, set()
            connected = self._connected.copy()
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
            return boards
        return [(slug, path) for slug, path in boards
                if (slug, path) in new or slug in dirty
                or (poll and (slug not in connected or slug in self._failed)) or pending([path])]
