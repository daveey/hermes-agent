"""In-process scoped transport for incremental serve cutover. [daveey]

Startup supplies authorized scopes and an owner-side demand queue. This module
performs no discovery, socket acquisition, source capture, or automatic release.
The data plane is the same immutable FactStore used by the out-of-process host.
"""
from contextvars import copy_context
import logging
import threading
import time

from hermes_cli.fact_expiry import ExpiryWorker
from hermes_cli.fact_local_reader import LocalHandle, LocalReader
from hermes_cli.fact_participant import ProcessLifecycle


class IncrementalLifecycle:
    """Independent release authority per explicitly authorized consumer/scope.

    Each consumer's existing callback owns its admission/drain fence. No process
    other than this serve participates; this is NOT the distributed global latch.
    """
    def __init__(self, expected):
        self._participants = {key: ProcessLifecycle({key}) for key in expected}
        if not self._participants:
            raise ValueError('empty participant roster')

    def _participant(self, name, scope):
        try:
            return self._participants[scope, name]
        except KeyError:
            raise ValueError('unexpected consumer') from None

    def bind_consumer(self, name, scope, callback):
        return self._participant(name, scope).bind_consumer(name, scope, callback)

    def report_ready(self, name, scope, *, version=1, ready=True):
        self._participant(name, scope).report_ready(name, scope, version=version, ready=ready)

    def transition(self, name, scope, mode, generation):
        self._participant(name, scope).transition(mode, generation)

    def has_bindings(self, scope):
        return any(p.has_bindings(scope) for p in self._participants.values())


class _SharedDelivery:
    """Store-owned worker custody, populated only by acquired transports."""

    def __init__(self, store):
        self.store = store
        self.owners = set()
        self._stopping = False
        self._retry = {}
        self._thread = threading.Thread(target=self._run,
                                        name='fact-local-delivery', daemon=True)
        self._expiry = ExpiryWorker(store)
        try:
            self._thread.start()
        except RuntimeError:
            self._expiry.close()
            raise

    def check_running(self):
        if (self._stopping or not self._thread.is_alive()
                or not self._expiry.thread.is_alive()):
            raise RuntimeError('local fact owner closed or worker unavailable')

    def _next(self):
        with self.store._condition:
            while not self._stopping and any(not owner._stopping for owner in self.owners):
                now = time.monotonic()
                readers = [reader for owner in self.owners for reader in owner._readers]
                for reader in readers:
                    reader.retiring = {s for s in reader.retiring if s.delivery_lock.locked()}
                contexts = {s.subscription: r.owner._context for r in readers for s in r.subscriptions
                            if not s.subscription.closed}
                self._retry = {s: pair for s, pair in self._retry.items() if s in contexts}
                pending = [s for s in contexts if s.queue or s.reset_pending]
                ready = [(s, contexts[s]) for s in pending if self._retry.get(s, (0, 0))[0] <= now]
                if ready:
                    return ready
                waits = [self._retry[s][0] - now for s in pending if s in self._retry]
                self.store._condition.wait(min(waits) if waits else None)
        return ()

    def _run(self):
        while subscriptions := self._next():
            for subscription, context in subscriptions:
                if not subscription.delivery_lock.acquire(blocking=False):
                    self._retry[subscription] = (time.monotonic() + .1, 0)
                    continue
                try:
                    context.run(subscription._deliver)
                    self._retry.pop(subscription, None)
                except Exception:  # health: allow BLE001 -- subscriber isolation retains reset with bounded retry
                    count = self._retry.get(subscription, (0, 0))[1]
                    delay = (.1, .2, .4, 1., 2., 5.)[min(count, 5)]
                    self._retry[subscription] = (time.monotonic() + delay, min(count + 1, 5))
                    logging.getLogger(__name__).exception('local fact callback failed; reset retained')
                finally:
                    subscription.delivery_lock.release()


class InProcessFacts:
    """Sleeping delivery and independent expiry workers; no IPC or process spawn.

    Callbacks run off publisher/request threads and outside publication locks.
    Failed callbacks retain reset custody with exponential .1–5s retry. Readers
    fail closed if a worker dies. Closing requires all readers be released.
    """
    def __init__(self, store, scopes, *, demand):
        self.store, self.scopes, self.demand = store, frozenset(scopes), demand
        if not self.scopes:
            raise ValueError('empty authorized scope set')
        self._readers = set()
        self._stopping = False
        self._context = copy_context()
        self._workers = None

    @property
    def _thread(self):
        return self._workers._thread if self._workers is not None else None

    @property
    def _expiry(self):
        return self._workers._expiry if self._workers is not None else None

    @property
    def _retry(self):
        return self._workers._retry if self._workers is not None else {}

    def check_control_thread(self):
        if threading.current_thread() is self._thread:
            raise RuntimeError('cannot close handles/owner from delivery thread')

    def check_running(self):
        workers = self._workers
        if self._stopping or workers is None:
            raise RuntimeError('local fact owner closed or worker unavailable')
        workers.check_running()

    def acquire(self, scope, *, lifecycle):
        with self.store._condition:
            if self._stopping:
                raise RuntimeError('local fact owner closed or worker unavailable')
            if scope not in self.scopes:
                raise ValueError('unauthorized_scope')
            if self._workers is None:
                workers = getattr(self.store, '_local_delivery', None)
                if workers is None:
                    # Creation and registration share the condition, so the new
                    # worker cannot observe an empty owner roster at startup.
                    workers = _SharedDelivery(self.store)
                    self.store._local_delivery = workers
                # Refused candidates retain their existing drain custody, but
                # must not poison this dormant transport's next admission.
                workers.check_running()
                workers.owners.add(self)
                self._workers = workers
            self.check_running()
            reader = LocalReader(self, scope)
            self._readers.add(reader)
            self.store._condition.notify_all()
            return LocalHandle(reader, lifecycle)

    def close(self, timeout=5):
        self.check_control_thread()
        with self.store._condition:
            if self._readers:
                raise RuntimeError('local owner retains reader handles')
            self._stopping = True
            workers = self._workers
            if workers is None or self not in workers.owners:
                return
            if len(workers.owners) > 1:
                workers.owners.remove(self)
                self.store._condition.notify_all()
                return
            workers._stopping = True
            self.store._condition.notify_all()
        workers._thread.join(timeout)
        if workers._thread.is_alive():
            raise TimeoutError('local delivery did not stop; custody retained')
        workers._expiry.close()
        if workers._expiry.thread.is_alive():
            raise TimeoutError('local expiry did not stop; custody retained')
        with self.store._condition:
            workers.owners.discard(self)
            if getattr(self.store, '_local_delivery', None) is workers:
                self.store._local_delivery = None
