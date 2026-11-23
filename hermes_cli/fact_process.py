"""Process-custodied fact storage, leased capture scheduling and lazy control. [daveey]

A lease drains only its own registrations. Captures and callbacks retain their
registration context and budget; the shared scheduler itself has no profile.
"""
from collections import deque
from contextvars import Context, copy_context
import logging
import threading

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_feed import ChangeFeed
from hermes_cli.fact_store import FactStore

_lock = threading.RLock()
_store = None
_feed = None
_leases = set()
_scopes = {}
_draining = None
_control = threading.Condition()
_queue = deque()
_pending = set()
_worker = None


def store():
    global _store
    with _lock:
        if _store is None:
            _store = FactStore()
        return _store


class _Adapter:
    def __init__(self, adapter, owner):
        self.adapter, self.owner = adapter, owner
        self.context = copy_context()
        self.condition = threading.Condition()
        self.capturing = self.closed = False
        self.watch_handle = None

    def watch(self, wake):
        self.watch_handle = self.context.run(self.adapter.watch, wake)
        return self

    def capture(self, cursor, ignored_budget):
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

    def publish(self, callback, value):
        with self.condition:
            if not self.closed:
                self.context.run(callback, self.adapter, value)

    def stop(self):
        with self.condition:
            self.closed = True

    def fence(self, timeout):
        with self.condition:
            if not self.condition.wait_for(lambda: not self.capturing, timeout):
                raise TimeoutError('source capture did not stop; owner custody retained')

    def close(self):
        if self.watch_handle is not None:
            self.context.run(self.watch_handle.close)


def _commit(adapter, capture):
    adapter.publish(adapter.owner.commit, capture)


def _failure(adapter, error):
    adapter.publish(adapter.owner.failure, error)


class FeedLease:
    """A capture owner's registrations on the single process scheduler."""
    def __init__(self, commit, failure, *, budget=CaptureBudget(), stores=None):
        global _feed
        self._registrations = []
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._stores = dict(stores or {})
        self.commit, self.failure, self.budget = commit, failure, budget
        with _lock:
            if _draining is not None:
                raise RuntimeError('fact scheduler drain custody retained')
            keys = [(id(storage), scope) for scope, storage in self._stores.items()]
            if any(key in _scopes for key in keys):
                raise RuntimeError('fact scope custody already retained')
            if _feed is None:
                _feed = Context().run(ChangeFeed, _commit, _failure)
            self._feed = _feed
            _leases.add(self)
            _scopes.update((key, self) for key in keys)

    @property
    def _thread(self):
        return self._feed._thread

    def register(self, adapter):
        with self._lock:
            if self._closed:
                raise RuntimeError('fact feed lease closed')
            bound = _Adapter(adapter, self)
            registration = self._feed.register(bound)
            self._registrations.append((bound, registration))
            return registration

    def close(self, timeout=5):
        if threading.current_thread() is self._thread:
            raise RuntimeError('cannot close fact feed from capture thread')
        with self._close_lock:
            self._close(timeout)

    def _close(self, timeout):
        global _feed, _draining
        # Do not hold the process lock while joining captures/watches: callbacks
        # may enqueue work or close unrelated leases.
        with self._lock:
            self._closed = True
            registrations = tuple(self._registrations)
        for bound, _registration in registrations:
            bound.stop()
        for bound, _registration in registrations:
            bound.fence(timeout)
        for bound, registration in registrations:
            registration.close()
            self._registrations.remove((bound, registration))
        with _lock:
            if self not in _leases:
                return
            last = len(_leases) == 1
            if last:
                _draining = self
            else:
                self._retire()
                return
        self._feed.close(timeout)
        with _lock:
            self._retire()
            _feed = None
            _draining = None

    def _retire(self):
        # Keep the process store stable: inert consumers may already hold it.
        # Retire only this tenure's facts before admitting its successor.
        for scope, storage in self._stores.items():
            storage.publish(deleted_keys=[key for key in storage.snapshot(scope)._facts if key.scope == scope])
        _leases.discard(self)
        for scope, storage in self._stores.items():
            if _scopes.get((id(storage), scope)) is self:
                del _scopes[id(storage), scope]


def submit(key, callback):
    """Bounded memory-only activation admission; duplicate work is coalesced."""
    global _worker
    with _control:
        if key in _pending:
            return
        if len(_pending) >= 256:
            raise RuntimeError('fact activation capacity exceeded')
        _pending.add(key)
        _queue.append((key, copy_context(), callback))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=lambda: Context().run(_activate),
                                       name='fact-activation', daemon=True)
            _worker.start()
        _control.notify_all()


def _activate():
    global _worker
    while True:
        with _control:
            if not _queue:
                _worker = None
                return
            key, context, callback = _queue.popleft()
        try:
            context.run(callback)
        except Exception:  # health: allow BLE001 -- activation failure remains fail-closed at the binding
            logging.getLogger(__name__).exception('fact activation failed')
        finally:
            with _control:
                _pending.discard(key)
