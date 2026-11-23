"""Bounded subscriber mailboxes; callbacks are explicitly drained off producer. [daveey]"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
import threading

from hermes_cli.fact_contract import Delta, FactKey, Position, Scope


if TYPE_CHECKING:
    from hermes_cli.fact_store import FactStore


@dataclass(eq=False)
class Subscription:
    store: FactStore
    scope: Scope
    kinds: frozenset[str]
    keys: frozenset[FactKey] | None
    callback: Callable[[Delta], None]
    position: Position | None
    max_envelopes: int
    max_bytes: int
    queue: deque[tuple[Delta, int]] = field(default_factory=deque)
    queued_bytes: int = 0
    reset_pending: bool = False
    closed: bool = False
    delivery_lock: threading.Lock = field(default_factory=threading.Lock)

    def enqueue(self) -> None:
        """Called under the store publication lock; never calls subscriber code."""
        if self.closed:
            return
        if self.reset_pending:
            return  # drain will take one current scoped snapshot
        if self.store._needs_reset(self.position):
            self.queue.clear()
            self.queued_bytes = 0
            self.reset_pending = True
            return
        delta = self.store._changes(self.scope, self.position, self.kinds, self.keys)
        size = self.store._delta_size(delta)
        self.position = Position(delta.epoch, delta.rev)
        if len(self.queue) >= self.max_envelopes or self.queued_bytes + size > self.max_bytes:
            self.queue.clear()
            self.queued_bytes = 0
            self.reset_pending = True
        else:
            self.queue.append((delta, size))
            self.queued_bytes += size

    def drain(self) -> None:
        """Deliver a bounded turn, with all callbacks outside publication locks."""
        if not self.delivery_lock.acquire(blocking=False):
            return
        try:
            self._deliver()
        finally:
            self.delivery_lock.release()

    def _deliver(self):
        from hermes_cli.fact_store import reset_delta
        snapshot = None
        with self.store._condition:
            if self.closed:
                return
            if self.reset_pending:
                snapshot = self.store.snapshot(self.scope)
                self.position = Position(snapshot.epoch, snapshot.rev)
                pending = ()
                self.reset_pending = False
            else:
                pending = tuple(delta for delta, _size in self.queue)
            self.queue.clear()
            self.queued_bytes = 0
        for delta in pending:
            self._callback(delta)
        if snapshot is not None:
            self._callback(reset_delta(snapshot, self.kinds, self.keys))

    def _callback(self, delta):
            if self.closed:
                return
            try:
                self.callback(delta)
            except Exception:
                with self.store._condition:
                    self.queue.clear()
                    self.queued_bytes = 0
                    self.reset_pending = True
                raise

    def close(self) -> None:
        with self.store._condition:
            self.closed = True
            self.queue.clear()
            self.queued_bytes = 0
            self.store._subscribers.discard(self)
