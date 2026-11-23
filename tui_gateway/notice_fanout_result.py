"""Action-pass scheduling receipt; never a substitute for durable ACK state."""
from __future__ import annotations

import time


class FanoutResult(list[dict]):
    """Legacy row list plus explicit retry obligations in monotonic seconds.

    An empty list alone never certifies quiet. Consumers must inspect ``quiet``
    and retain ``next_deadline`` even when this list is falsey.
    """

    def __init__(self):
        super().__init__()
        self.incomplete = False
        self.next_deadline: float | None = None

    @property
    def quiet(self) -> bool:
        """The pass drained its captured inputs, whether or not it acted."""
        return not self.incomplete

    def retry(self, delay: float = 1.0) -> None:
        self.incomplete = True
        deadline = time.monotonic() + delay
        self.next_deadline = deadline if self.next_deadline is None else min(self.next_deadline, deadline)

    def absorb(self, other: FanoutResult) -> None:
        self.extend(other)
        if other.incomplete:
            self.incomplete = True
            if other.next_deadline is not None:
                self.next_deadline = (other.next_deadline if self.next_deadline is None
                                      else min(self.next_deadline, other.next_deadline))
