"""Board-specific feed invalidation, with a fixed full-recovery deadline."""
from __future__ import annotations

import threading
from pathlib import Path
from collections.abc import Callable


class SyncWork:
    def __init__(self, period: float) -> None:
        self.period = period
        self.deadline = 0.0
        self._lock = threading.Lock()
        self._dirty: set[str] = set()
        self._known: set[tuple[str, Path]] = set()

    def changed(self, board: str) -> None:
        with self._lock:
            self._dirty.add(board)

    def select(self, boards: list[tuple[str, Path]], now: float,
               pending: Callable[[list[Path]], bool]) -> list[tuple[str, Path]]:
        # Consume before starting IO: an event arriving during that IO remains dirty
        # for the next round, even if its event notification is coalesced.
        with self._lock:
            dirty, self._dirty = self._dirty, set()
        current = set(boards)
        new = current - self._known
        self._known = current
        if now >= self.deadline:
            self.deadline = now + self.period
            return boards
        return [(slug, path) for slug, path in boards
                if (slug, path) in new or slug in dirty or pending([path])]
