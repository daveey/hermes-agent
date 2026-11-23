"""Scoped links-health invalidations for the existing unloaded executor. [daveey]

Publication does no I/O and starts no worker. A failed/recovered source invalidates
canonical outcomes even when every retained card's last status stayed unchanged.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

_lock = threading.RLock()
_health: dict[tuple, tuple[int, bool]] = {}
_pending: dict[tuple, int] = {}
_watches: set[HealthWatch] = set()


def changed(origin: tuple, available: bool) -> None:
    with _lock:
        old = _health.get(origin)
        if old is not None and old[1] == available:
            return
        generation = old[0] + 1 if old else 1
        _health[origin] = (generation, available)
        _pending[origin] = generation
        # Wake callbacks are Event.set only, never refresh/DB work.
        for watch in tuple(_watches):
            watch.wake()


def pending(origin: tuple) -> int | None:
    with _lock:
        return _pending.get(origin)


def acknowledge(origin: tuple, generation: int | None, *, complete: bool = True) -> None:
    with _lock:
        if complete and generation is not None and _pending.get(origin) == generation:
            _pending.pop(origin)


class HealthWatch:
    def __init__(self, wake: Callable[[], None]):
        self.wake = wake
        self.seen: dict[tuple, int] = {}
        with _lock:
            _watches.add(self)

    def due(self, homes: set[str]) -> bool:
        with _lock:
            current = {origin: value[0] for origin, value in _health.items() if origin[0] in homes}
        dirty = current != self.seen
        self.seen = current
        return dirty

    def close(self) -> None:
        with _lock:
            _watches.discard(self)


def reset_for_tests() -> None:
    with _lock:
        _health.clear()
        _pending.clear()
        _watches.clear()
