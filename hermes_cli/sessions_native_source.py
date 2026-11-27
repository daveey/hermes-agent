"""Native invalidation of demanded canonical session display captures. [daveey]

The canonical reader runs on ChangeFeed, NOT inside a reducer or HTTP request.
Source vectors identify captures, not a cross-database transaction. External
annotations/time/roster have a disclosed 30s reconciliation bound. One fair queue
retains trailing hints; a failed selector cannot starve unrelated selectors.
"""
from collections import OrderedDict
from dataclasses import dataclass
import json
import logging
import threading
import time

from hermes_cli.fact_capture import CaptureBudgetExceeded
from hermes_cli.fact_contract import Fact, evolve
from hermes_cli.fact_source_watch import NativeSourceWatch
from hermes_cli.fact_store import view_key
from hermes_cli.sessions_capture_timing import CaptureTimingLog

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionViewCapture:
    after: int
    next_deadline: float
    fact: object = None
    facts: tuple = ()


class SessionsSource:
    """Bounded per-endpoint inventories with read-touch LRU and fair dirty order."""
    def __init__(self, store, source, paths, capture, *, max_views=32,
                 max_bytes=12 * 1024 * 1024, refresh_seconds=30, reconcile=None, capture_many=None):
        self.store, self.source = store, source
        self.paths, self.reader = tuple(paths), capture
        self.reader_many = capture_many
        self.max_views, self.max_bytes = max_views, max_bytes
        self.refresh_seconds, self.reconcile = refresh_seconds, reconcile
        self._lock = threading.Lock()
        self._selectors = OrderedDict()
        self._dirty = OrderedDict()
        self._capturing = set()
        self._sizes, self._retry = {}, {}
        self._failed = set()
        self._retired = set()
        self._watch = self._wake = None
        self._closed = False
        self._serial = 0
        self._next_capture = 0
        self._refresh = time.monotonic() + refresh_seconds
        self._timings = CaptureTimingLog.from_environment()
        self._capture_seq = 0

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
        self._capturing.discard(key)
        self._retry.pop(key, None)
        self._failed.discard(key)
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
            # Pending HTTP reads join the capture already admitted for this key.
            # Only real invalidation may request its trailing refresh. Keep this
            # fence through commit: a completed reader is not yet a Ready view.
            if key not in self._capturing:
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
            keys = [key for key in self._dirty
                    if started >= max(self._next_capture, self._retry.get(key, 0))]
            keys = keys[:4 if self.reader_many is not None else 1]
            if self.reader_many is not None:
                from hermes_cli.sessions_capture_process import MAX_REQUEST
                while len(keys) > 1 and len(json.dumps(
                        {'views': [self._selectors[key] for key in keys]}, allow_nan=False).encode()) > MAX_REQUEST:
                    keys.pop()  # Keep long stamp selectors fair; never overload a shared frame.
            if not keys:
                return SessionViewCapture(self._serial, self._deadline())
            views = [self._selectors[key] for key in keys]
            for key in keys:
                self._dirty.pop(key)
            self._capturing.update(keys)
        # The feed retains the launch profile's context. Teach its long-lived
        # holder this scope once per batch; a child's demand cannot do that.
        # Do not pull annotation imports into early sessions bootstrap.
        import sys
        try:
            ksa = sys.modules.get('hermes_cli.kanban_session_activity')
            if ksa is not None:
                ksa._host_copy.demand(ksa._origin_scope())
        except Exception:  # health: allow BLE001 -- advisory holder demand must never fail capture
            logger.debug('sessions annotation demand unavailable', exc_info=True)
        try:
            bodies = (self.reader_many(views) if self.reader_many is not None else
                      [self.reader(*views[0])])
            if len(bodies) != len(keys):
                raise ValueError('invalid batch result count')
            if sum(len(body) for body in bodies if isinstance(body, bytes)) > min(budget.max_bytes, self.max_bytes):
                from hermes_cli.sessions_capture_process import CaptureUnavailable
                raise CaptureUnavailable('bytes')
        except Exception as error:  # health: allow BLE001 -- transport fault invalidates each selected view
            bodies = [error] * len(keys)
        facts = tuple(self._fact(key, body, started, budget) for key, body in zip(keys, bodies))
        timing = None
        with self._lock:
            # <=50% duty charges the WHOLE batch, including spawn and serialization. One exception:
            # the boot standby child was spawned before its batch began, so that one-time wall is
            # not charged here; its CPU still is recorded (sessions.capture.child), used or not.
            ended = time.monotonic()
            self._next_capture = ended + max(.2, ended - started)
            due = self._deadline()
            if self._timings is not None:
                self._capture_seq += 1
                timing = (self._capture_seq, self._serial, self._next_capture)
        capture = SessionViewCapture(self._serial, due, facts[0], facts)
        if self._timings is not None and timing is not None:
            seq, serial_last, notbefore = timing
            self._timings.emit('sessions.capture.timing', seq=seq,
                               serial_first=serial_last - len(facts) + 1, serial_last=serial_last,
                               selected=len(facts), failed=sum(bool(f.error_code) for f in facts),
                               started=started, ended=ended, notbefore=notbefore)
        return capture

    def _fact(self, key, body, started, budget):
        observed = time.time()
        self._serial += 1
        reason, encoded_bytes = 'reader_error', None
        try:
            if isinstance(body, Exception):
                raise body
            encoded_bytes = len(body) if isinstance(body, bytes) else None
            reason = ('invalid_type' if encoded_bytes is None else
                      'bytes' if encoded_bytes > min(budget.max_bytes, self.max_bytes) else
                      'elapsed' if time.monotonic() - started > budget.max_seconds else None)
            if reason is not None:
                raise CaptureBudgetExceeded('sessions view exceeds capture budget')
            fact = Fact(key, {'encoded': body}, source_vector=((self.source, self._serial),),
                        observed_at=observed, valid_until=observed + 2 * self.refresh_seconds,
                        health='fresh')
        except Exception as error:  # health: allow BLE001 -- isolate capture failures; unknown is not empty success
            from hermes_cli.sessions_capture_process import CaptureUnavailable
            if isinstance(error, CaptureUnavailable):
                reason = error.reason
            with self._lock:
                report = key in self._selectors and key not in self._failed
                if report:
                    self._failed.add(key)
            if report:
                # No selector, path, payload or exception text; once per failure episode.
                logger.warning('sessions capture unavailable reason=%s elapsed_seconds=%.3f encoded_bytes=%s',
                               reason or 'reader_error', time.monotonic() - started, encoded_bytes)
            old = self.store.snapshot(self.source.scope).get(key)
            fact = (evolve(old, health='stale', error_code='sessions_capture_failed') if old is not None
                    else Fact(key, {}, error_code='sessions_capture_failed'))
            with self._lock:
                if key in self._selectors:
                    self._dirty.setdefault(key, None)
                    self._retry[key] = time.monotonic() + 5
        else:
            with self._lock:
                self._retry.pop(key, None)
                self._failed.discard(key)
        return fact

    def commit(self, capture):
        with self._lock:
            deleted = set(self._retired)
            self._retired.clear()
            facts = capture.facts or ((capture.fact,) if capture.fact is not None else ())
            for key in deleted:
                self._sizes.pop(key, None)
            retained = []
            for fact in facts:
                if fact.key not in self._selectors:
                    continue  # Evicted during capture or earlier in this batch.
                size = len(fact.value.get('encoded', b''))
                while sum(self._sizes.values()) - self._sizes.get(fact.key, 0) + size > self.max_bytes:
                    victim = next(key for key in self._selectors if key != fact.key and key in self._sizes)
                    self._evict(victim)
                    self._sizes.pop(victim)
                    deleted.add(victim)
                self._sizes[fact.key] = size
                retained.append(fact)
            self.store.publish([fact for fact in retained if fact.key in self._selectors], deleted_keys=deleted)
            self._capturing.difference_update(fact.key for fact in facts)

    def failure(self):
        # Watch/publication failure (not one selector failure) invalidates all.
        snapshot = self.store.snapshot(self.source.scope)
        with self._lock:
            keys = tuple(self._selectors)
            self._capturing.clear()
            for key in keys:
                self._dirty.setdefault(key, None)
        stale = [evolve(fact, health='stale', error_code='sessions_source_failed')
                 for key in keys if (fact := snapshot.get(key)) is not None]
        self.store.publish(stale)

    def close(self):
        with self._lock:
            self._closed = True
        if self._watch is not None:
            self._watch.close()
