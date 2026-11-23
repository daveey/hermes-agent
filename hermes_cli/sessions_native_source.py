"""Native invalidation of demanded canonical session display captures. [daveey]

The canonical reader runs on ChangeFeed, NOT inside a reducer or HTTP request.
Source vectors identify captures, not a cross-database transaction. External
annotations/time/roster have a disclosed 30s reconciliation bound. One fair queue
retains trailing hints; a failed selector cannot starve unrelated selectors.
"""
from collections import OrderedDict
from dataclasses import dataclass, replace
import threading
import time

from hermes_cli.fact_capture import CaptureBudgetExceeded
from hermes_cli.fact_contract import Fact
from hermes_cli.fact_source_watch import NativeSourceWatch
from hermes_cli.fact_store import view_key


@dataclass(frozen=True)
class SessionViewCapture:
    after: int
    next_deadline: float
    fact: object = None


class SessionsSource:
    """Bounded per-endpoint inventories with read-touch LRU and fair dirty order."""
    def __init__(self, store, source, paths, capture, *, max_views=32,
                 max_bytes=12 * 1024 * 1024, refresh_seconds=30, reconcile=None):
        self.store, self.source = store, source
        self.paths, self.reader = tuple(paths), capture
        self.max_views, self.max_bytes = max_views, max_bytes
        self.refresh_seconds, self.reconcile = refresh_seconds, reconcile
        self._lock = threading.Lock()
        self._selectors = OrderedDict()
        self._dirty = OrderedDict()
        self._sizes, self._retry = {}, {}
        self._retired = set()
        self._watch = self._wake = None
        self._closed = False
        self._serial = 0
        self._next_capture = 0
        self._refresh = time.monotonic() + refresh_seconds

    def watch(self, wake):
        with self._lock:
            if self._wake is not None or self._closed:
                raise RuntimeError('sessions source already watched or closed')
            self._wake = wake
        self._watch = NativeSourceWatch(self.paths, self.invalidate)
        return self

    def _evict(self, key):
        self._selectors.pop(key)
        self._dirty.pop(key, None)
        self._retry.pop(key, None)
        self._retired.add(key)

    def touch(self, name, selector):
        key = view_key(self.source.scope, name, selector)
        with self._lock:
            if key in self._selectors:
                self._selectors.move_to_end(key)

    def demand(self, name, selector):
        key = view_key(self.source.scope, name, selector)
        with self._lock:
            if self._closed:
                raise RuntimeError('sessions source closed')
            if key not in self._selectors:
                peers = [k for k, (view, _) in self._selectors.items() if view == name]
                if len(peers) >= self.max_views:
                    self._evict(peers[0])
                self._selectors[key] = (name, dict(selector))
            self._selectors.move_to_end(key)
            self._dirty.setdefault(key, None)
            wake = self._wake
        if wake is not None:
            wake()  # ChangeFeed condition only; no file descriptor writes.

    def invalidate(self):
        with self._lock:
            for key in self._selectors:
                self._dirty.setdefault(key, None)
            wake = self._wake
        if wake is not None:
            wake()

    def refresh_at(self, wall_deadline):
        # A capture already observes elapsed boundaries. Rearming one at now
        # feeds its own publication straight back into capture forever when a
        # canonical row still carries an active annotation after that boundary.
        delay = wall_deadline - time.time()
        if delay <= 0:
            return
        with self._lock:
            self._refresh = min(self._refresh, time.monotonic() + delay)

    def _deadline(self):
        deadlines = [max(self._next_capture, self._retry.get(key, 0)) for key in self._dirty]
        return min([self._refresh, *deadlines])

    def capture(self, cursor, budget):
        self._watch.check()
        started = time.monotonic()
        if started >= self._refresh and self.reconcile is not None:
            self.reconcile()  # owner-only roster discovery, never demand/request
        with self._lock:
            if started >= self._refresh:
                for key in self._selectors:
                    self._dirty.setdefault(key, None)
                self._refresh = started + self.refresh_seconds
            key = next((key for key in self._dirty
                        if started >= max(self._next_capture, self._retry.get(key, 0))), None)
            if key is None:
                return SessionViewCapture(self._serial, self._deadline())
            self._dirty.pop(key)
            name, selector = self._selectors[key]
        observed = time.time()
        self._serial += 1
        try:
            body = self.reader(name, selector)
            if (not isinstance(body, bytes) or len(body) > min(budget.max_bytes, self.max_bytes)
                    or time.monotonic() - started > budget.max_seconds):
                raise CaptureBudgetExceeded('sessions view exceeds capture budget')
            fact = Fact(key, {'encoded': body}, source_vector=((self.source, self._serial),),
                        observed_at=observed, valid_until=observed + 2 * self.refresh_seconds,
                        health='fresh')
        except Exception:  # health: allow BLE001 -- isolate capture failures; unknown is not empty success
            old = self.store.snapshot(self.source.scope).get(key)
            fact = (replace(old, health='stale', error_code='sessions_capture_failed') if old is not None
                    else Fact(key, {}, error_code='sessions_capture_failed'))
            with self._lock:
                if key in self._selectors:
                    self._dirty.setdefault(key, None)
                    self._retry[key] = time.monotonic() + 5
        else:
            with self._lock:
                self._retry.pop(key, None)
        with self._lock:
            # <=50% capture duty under sustained hints, at most five passes/sec.
            self._next_capture = time.monotonic() + max(.2, time.monotonic() - started)
            due = self._deadline()
        return SessionViewCapture(self._serial, due, fact)

    def commit(self, capture):
        with self._lock:
            deleted = set(self._retired)
            self._retired.clear()
            fact = capture.fact
            if fact is not None and fact.key not in self._selectors:
                fact = None  # Evicted during capture.
            for key in deleted:
                self._sizes.pop(key, None)
            if fact is not None:
                size = len(fact.value.get('encoded', b''))
                while sum(self._sizes.values()) - self._sizes.get(fact.key, 0) + size > self.max_bytes:
                    victim = next(key for key in self._selectors if key != fact.key and key in self._sizes)
                    self._evict(victim)
                    self._sizes.pop(victim)
                    deleted.add(victim)
                self._sizes[fact.key] = size
            self.store.publish([fact] if fact is not None else (), deleted_keys=deleted)

    def failure(self):
        # Watch/publication failure (not one selector failure) invalidates all.
        snapshot = self.store.snapshot(self.source.scope)
        with self._lock:
            keys = tuple(self._selectors)
            for key in keys:
                self._dirty.setdefault(key, None)
        stale = [replace(fact, health='stale', error_code='sessions_source_failed')
                 for key in keys if (fact := snapshot.get(key)) is not None]
        self.store.publish(stale)

    def close(self):
        with self._lock:
            self._closed = True
        if self._watch is not None:
            self._watch.close()
