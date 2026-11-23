"""Bounded view demand mailbox executed by the existing ChangeFeed. [daveey]

No SQLite capture, extra thread or polling. Demands remain resident until owner
shutdown (bounded by admission); reference-counted eviction is not implemented.
Only the feed thread binds/reduces. Failed publication retains the queue head.
"""
from collections import deque
from dataclasses import dataclass
import threading
import time

from hermes_cli.fact_store import view_key


@dataclass(frozen=True)
class DemandCapture:
    key: object
    after: int
    more: bool

    @property
    def next_deadline(self):
        # Read by the scheduler AFTER commit, yielding to hints during reduction.
        return time.monotonic() if self.more else None


class DemandSource:
    def __init__(self, engine, *, max_keys=4096, max_selector_bytes=16384):
        if type(max_keys) is not int or max_keys < 1 or type(max_selector_bytes) is not int or max_selector_bytes < 1:
            raise ValueError('demand bounds must be positive integers')
        self.engine = engine
        self.max_keys, self.max_selector_bytes = max_keys, max_selector_bytes
        self._lock = threading.Lock()
        self._accepted = set()
        self._pending = deque()
        self._wake = None
        self._closed = False

    def watch(self, wake):
        with self._lock:
            if self._closed or self._wake is not None:
                raise RuntimeError('demand mailbox closed or already watched')
            self._wake = wake
        return self

    def demand(self, view, selector):
        key = view_key(self.engine.scope, view, selector)
        if len(key.id.encode()) > self.max_selector_bytes:
            raise ValueError('selector exceeds demand bound')
        # Specs are immutable after startup; validation does not bind or reduce.
        spec = self.engine._specs.get('ViewFact')
        if spec is None:
            raise ValueError('unregistered view reducer')
        if spec.validate is not None:
            spec.validate(key)
        with self._lock:
            if self._closed or self._wake is None:
                raise RuntimeError('demand mailbox closed')
            if key in self._accepted:
                return
            if len(self._accepted) >= self.max_keys:
                raise ValueError('demand capacity exceeded')
            self._accepted.add(key)
            self._pending.append(key)
            wake = self._wake
        wake()

    def capture(self, cursor, budget):
        with self._lock:
            # Registration boot and redundant hints may arrive with no demand.
            key = self._pending[0] if self._pending else None
            return DemandCapture(key, (cursor or 0) + 1, len(self._pending) > 1)

    def commit(self, capture):
        if capture.key is None:
            return
        newly_bound = capture.key not in self.engine._bound
        if newly_bound:
            self.engine.bind(capture.key)
        try:
            self.engine.publish(now=time.time())
        except Exception:  # health: allow BLE001 -- undo speculative binding before propagating publication failure
            if newly_bound:
                self.engine.unbind(capture.key)
            raise
        with self._lock:
            if not self._pending or self._pending[0] != capture.key:
                raise RuntimeError('demand publication order mismatch')
            self._pending.popleft()

    def failure(self):
        # Retry remains bounded by ChangeFeed. Rotate a refused demand so other
        # selectors can progress (e.g. a small view behind an oversized one).
        # Failed speculative bindings have already been removed by commit.
        with self._lock:
            if self._pending:
                self._pending.rotate(-1)

    def close(self):
        with self._lock:
            self._closed = True
            self._wake = None
