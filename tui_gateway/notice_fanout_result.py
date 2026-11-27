"""Action-pass scheduling receipt; never a substitute for durable ACK state."""
from __future__ import annotations

import time

from gateway.notification_retry_provenance import earliest, origin


class FanoutResult(list[dict]):
    """Legacy row list plus explicit retry obligations in monotonic seconds.

    An empty list alone never certifies quiet. Consumers must inspect ``quiet``
    and retain ``next_deadline`` even when this list is falsey.
    """

    def __init__(self, *, subpass='unknown'):
        super().__init__()
        self.incomplete = False
        self.next_deadline: float | None = None
        self.clock_deadline: float | None = None
        self.retry_origin = None
        self.subpass = origin(subpass)[0]

    @property
    def quiet(self) -> bool:
        """The pass drained its captured inputs, whether or not it acted."""
        return not self.incomplete

    def retry(self, delay: float = 1.0, *, subpass=None, reason='unknown') -> None:
        self.incomplete = True
        deadline = time.monotonic() + delay
        self.next_deadline = deadline if self.next_deadline is None else min(self.next_deadline, deadline)
        self.retry_origin = earliest(self.retry_origin, deadline, self.subpass if subpass is None else subpass, reason)

    def absorb(self, other: FanoutResult) -> None:
        self.extend(other)
        clock_deadline = getattr(other, 'clock_deadline', None)
        if clock_deadline is not None:
            self.clock_deadline = (clock_deadline if self.clock_deadline is None
                                   else min(self.clock_deadline, clock_deadline))
        if other.incomplete:
            self.incomplete = True
            if other.next_deadline is not None:
                self.next_deadline = (other.next_deadline if self.next_deadline is None
                                      else min(self.next_deadline, other.next_deadline))
            source = getattr(other, 'retry_origin', None)
            if source is not None:
                self.retry_origin = earliest(self.retry_origin, *source)

    def clock(self, wall_due):
        """A successful pass can owe a future semantic transition, not a retry."""
        if wall_due is not None:
            due = time.monotonic() + max(0., wall_due - time.time())
            self.clock_deadline = due if self.clock_deadline is None else min(self.clock_deadline, due)
