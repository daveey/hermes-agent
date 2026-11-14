"""Wake the restart-safe cron delivery drain when a worker writes the queue (#117307).

A restart-safe worker runs its job outside the gateway, queues the final send in
``cron/deliveries.db`` and polls that row once a second; the gateway drained the
queue only on its housekeeping tick, so a turn that ended in 8 s reached the user
a minute later. Between ticks the housekeeping thread now stats the queue file
(and its WAL) and drains as soon as either changed. The tick's own drain stays
as the fallback for anything the stat cannot see.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

# How often the housekeeping thread looks at the queue file between its ticks.
DELIVERY_QUEUE_WATCH_SECONDS = 1.0
DRAIN_LABEL = "Cron durable delivery queue drain"
# A file timestamp is only as fine as the clock that wrote it (a kernel jiffy on Linux, 1 s on
# some filesystems), and the queue's size does not move when a status update rewrites a page in
# place (each closed connection checkpoints and deletes the WAL). So a write that lands in the
# same granule as the mtime we last saw leaves the stamp identical. As in git's racy-index rule,
# a stamp taken this soon after its newest mtime proves nothing about writes after it.
MTIME_GRANULARITY_NS = 1_000_000_000


def _now_ns() -> int:
    return time.time_ns()


class DeliveryQueueWatch:
    """Stamp-compare every served profile's queue file; ``drain`` when a stamp moved.

    The watch never resets itself after draining: the drain's own status writes
    move the stamp once more and cost one empty pass, after which the stamp is
    stable, while a worker that enqueued during the drain still moves it again.
    A stamp seen within ``MTIME_GRANULARITY_NS`` of its newest mtime is racy: the next
    look reports a change even when the stamp matches (at worst one empty pass),
    because a write in the same timestamp granule would not have moved it. A newest mtime
    ahead of the clock (wall clock stepped back, skewed writer on a network FS) is racy only
    on the look that first sees it, or every poll would drain until the clock passed it.
    """

    def __init__(self, homes: Callable[[], Iterable[Optional[Path]]], drain: Callable[[], None]) -> None:
        self._homes = homes
        self.drain = drain
        self._served: list = list(homes())
        self._paths: dict = {}
        self._seen = self._stamp()
        self._seen_racy = self._is_racy(self._seen, first_sight=True)

    def refresh_homes(self) -> None:
        """Re-resolve the served profiles once per tick (a directory read under multiplex),
        not on every poll; a profile that appears mid-interval is drained by the tick itself."""
        self._served = list(self._homes())
        self._paths = {}

    def _queue_path(self, home: Optional[Path]) -> Path:
        """``queue_path(home)``, resolved once per tick: it realpaths the home (an lstat per path
        component), which the one-second poll otherwise repeats for every served profile."""
        from cron import delivery_queue

        key = (home, delivery_queue.DELIVERY_DB)
        path = self._paths.get(key)
        if path is None:
            path = self._paths[key] = delivery_queue.queue_path(home)
        return path

    def _stamp(self) -> tuple:
        stamps = []
        for home in self._served:
            path = self._queue_path(home)
            for candidate in (path, path.with_name(path.name + "-wal")):
                try:
                    info = os.stat(candidate)
                except OSError:
                    continue
                stamps.append((str(candidate), info.st_mtime_ns, info.st_size))
        return tuple(stamps)

    @staticmethod
    def _is_racy(stamp: tuple, first_sight: bool) -> bool:
        # Read the clock after the stat: any later write carries a timestamp past this granule.
        newest = max((mtime_ns for _path, mtime_ns, _size in stamp), default=None)
        if newest is None:
            return False
        age_ns = _now_ns() - newest
        return age_ns <= MTIME_GRANULARITY_NS and (age_ns >= 0 or first_sight)

    def changed(self) -> bool:
        current = self._stamp()
        if current == self._seen and not self._seen_racy:
            return False
        first_sight = current != self._seen
        self._seen = current
        self._seen_racy = self._is_racy(current, first_sight)
        return True


def wait_for_next_tick(stop_event, interval: float, watch: Optional[DeliveryQueueWatch], chore) -> None:
    """Sleep one housekeeping interval, draining the worker queue the moment it changes."""
    if watch is None:
        stop_event.wait(timeout=interval)
        return
    watch.refresh_homes()
    deadline = time.monotonic() + interval
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        if stop_event.wait(timeout=min(remaining, DELIVERY_QUEUE_WATCH_SECONDS)):
            return
        if watch.changed():
            chore(DRAIN_LABEL, watch.drain)
        if time.monotonic() >= deadline:
            return
