"""Parallel wake slots of the unloaded sweeper's passes, sized by the host loop lease. [daveey]

The unloaded sweeper (``kanban_unloaded``), the notice guarantee and the delegation pass each start at most a few
wake turns at once (an agent build may take minutes; the pass must not stall on it). Under the host loop lease
(``hermes_cli.host_loop_lease``, design t_01a1d45d75851804 §2b) ONE process per home runs the host pass for every
unloaded chat, where before every gateway/serve/dashboard process ran it with 2 slots each. The holder therefore
gets ``HOLDER_SLOTS`` (default 8, ``HERMES_KANBAN_HOLDER_WAKE_SLOTS``); a standby's residual pass, for chats live in
it, keeps the per-module base. With the lease off (``HERMES_HOST_LOOP_LEASE=0``) every process keeps its base, as
before.

``WakeSlots`` keeps ``threading.BoundedSemaphore``'s non-blocking ``acquire``/``release`` shape (tests swap one in);
lowering the limit never revokes a slot already taken, it only refuses new ones until enough are released.
"""

from __future__ import annotations

import os
import threading
import weakref

HOLDER_SLOTS_ENV = "HERMES_KANBAN_HOLDER_WAKE_SLOTS"  # health: allow HX002 -- the card's holder wake-capacity knob
HOLDER_SLOTS = 8

_ALL: weakref.WeakSet = weakref.WeakSet()


def holder_capacity() -> int:
    try:
        return max(1, int(os.environ.get(HOLDER_SLOTS_ENV, HOLDER_SLOTS)))
    except (TypeError, ValueError):
        return HOLDER_SLOTS


class WakeSlots:
    """A resizable non-blocking counter: ``acquire()`` takes one slot if fewer than ``limit`` are taken."""

    def __init__(self, base: int) -> None:
        self.base = int(base)
        self.limit = self.base
        self._used = 0
        self._mu = threading.Lock()
        _ALL.add(self)

    def acquire(self, blocking: bool = False) -> bool:  # ``blocking`` kept for the BoundedSemaphore call shape
        with self._mu:
            if self._used >= self.limit:
                return False
            self._used += 1
            return True

    def release(self) -> None:
        with self._mu:
            if self._used <= 0:
                raise ValueError("WakeSlots released too many times")
            self._used -= 1

    @property
    def used(self) -> int:
        return self._used


def set_holder(holder: bool) -> None:
    """Size every pass's slots for this process: the holder's capacity, else each one's own base."""
    capacity = holder_capacity()
    for slots in list(_ALL):
        slots.limit = capacity if holder else slots.base
