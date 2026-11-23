"""One process capture scheduler; independently drainable scoped registrations. [daveey]

The shared feed carries no ambient profile authority. Each registration captures
its caller context and budget. Closing one owner fences its publications and
waits for its actual captures, without stopping peers or forgetting failed drains.
"""
from contextvars import copy_context
import threading

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_feed import ChangeFeed
from hermes_cli.fact_store import FactStore

_lock = threading.RLock()
_feed = _store = None
_owners = set()


def process_store():
    global _store
    with _lock:
        if _store is None:
            _store = FactStore()
        return _store


class _Bound:
    def __init__(self, adapter, owner):
        self.adapter, self.owner = adapter, owner
        self.context = copy_context()
        self.condition = threading.Condition()
        self.capturing = self.closed = False
        self.watch_handle = None

    def watch(self, wake):
        self.watch_handle = self.adapter.watch(wake)
        return self

    def capture(self, cursor, budget):
        with self.condition:
            if self.closed:
                raise RuntimeError('source owner closed')
            self.capturing = True
        try:
            return self.context.run(self.adapter.capture, cursor, self.owner.budget)
        finally:
            with self.condition:
                self.capturing = False
                self.condition.notify_all()

    def commit(self, capture):
        with self.condition:
            if not self.closed:
                self.context.run(self.owner.commit, self.adapter, capture)

    def failure(self, error):
        with self.condition:
            if not self.closed:
                self.context.run(self.owner.failure, self.adapter, error)

    def fence(self, timeout):
        with self.condition:
            self.closed = True
            if not self.condition.wait_for(lambda: not self.capturing, timeout):
                raise TimeoutError('source capture did not stop; owner custody retained')

    def stop(self):
        with self.condition:
            self.closed = True

    def close(self):
        if self.watch_handle is not None:
            self.watch_handle.close()


class ProcessOwner:
    def __init__(self, commit, failure, *, budget=CaptureBudget()):
        global _feed
        self.commit, self.failure, self.budget = commit, failure, budget
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._registrations = []
        self._closed = False
        with _lock:
            if _feed is None:
                _feed = ChangeFeed(lambda source, capture: source.commit(capture),
                                   lambda source, error: source.failure(error))
            self._feed = _feed
            self._thread = _feed._thread
            _owners.add(self)

    def register(self, adapter):
        with self._lock:
            if self._closed:
                raise RuntimeError('source owner closed')
            bound = _Bound(adapter, self)
            registration = self._feed.register(bound)
            self._registrations.append((bound, registration))
            return registration

    def close(self, timeout=5):
        global _feed, _store
        if threading.current_thread() is self._thread:
            raise RuntimeError('cannot close capture owner on its feed thread')
        with self._close_lock:
            with self._lock:
                self._closed = True
                registrations = tuple(self._registrations)
            for bound, _registration in registrations:
                bound.stop()
            for bound, _registration in registrations:
                bound.fence(timeout)
            for _bound, registration in registrations:
                registration.close()
            self._registrations.clear()
            with _lock:
                if self not in _owners:
                    return
                if len(_owners) == 1:
                    self._feed.close(timeout)
                    _feed = _store = None
                _owners.remove(self)
