"""Bounded memory-only scope admission on the shared capture owner. [daveey]"""
from collections import deque
from contextvars import copy_context
import threading
import time
from types import SimpleNamespace


class ScopeActivation:
    def __init__(self, allowed, activate):
        self.allowed, self.activate = frozenset(allowed), activate
        self._lock = threading.Lock()
        self._pending, self._admitted = deque(), set()
        self._wake = None
        self._closed = False

    def want(self, scope):
        with self._lock:
            if self._closed or scope not in self.allowed or scope in self._admitted:
                return
            self._admitted.add(scope)
            self._pending.append((scope, copy_context()))
            wake = self._wake
        if wake is not None:
            wake()

    def watch(self, wake):
        with self._lock:
            self._wake = wake
        return self

    def capture(self, cursor, budget):
        with self._lock:
            item = self._pending.popleft() if self._pending and not self._closed else None
        if item is not None:
            scope, context = item
            # A partial activation retains registration/drain custody. Never
            # replace it on a later request; explicit owner close must succeed.
            context.run(self.activate, scope)
        with self._lock:
            deadline = time.monotonic() if self._pending else None
        return SimpleNamespace(after=cursor, next_deadline=deadline)

    def close(self):
        with self._lock:
            self._closed = True
            self._pending.clear()
            self._wake = None

    def commit(self, capture):
        pass

    def failure(self):
        pass
