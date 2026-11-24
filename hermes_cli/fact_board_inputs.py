"""Demanded native board/tree evidence on the existing ContentOwner feed. [daveey]

This adapter owns no task/session clock or route response cache. Trusted startup
supplies scoped native capture functions; the engine's ordinary view reducers
consume their detached inputs. Request demand only edits a bounded memory queue.
Each worker turn captures one selector, so a slow view cannot starve its peers.

Pacing (hub 2026-11-24): the source watch fires on every write to the board file and its SQLite sidecars, about
4/s on the hub's default board, and each firing re-queued every wanted selector, so the one shared
``fact-board-capture`` worker recaptured all views back to back (~90% of a core in the dashboard backend). The
capture deadline also ran from submission, so a capture queued behind other boards' captures on that shared worker
overran before it started, marking its evidence stale (``503 dependency_unavailable`` on the board route, ~20/min).
A selector now starts at most once per ``min_gap_seconds`` (writes inside the gap coalesce into one capture; the
legacy board snapshot refreshes at most every 20 s), and the deadline runs from the capture's own start. A
board capture is 0.7-0.8 s alone but 4-8 s cold and longer under the backend's GIL contention, so the owner's 10 s
budget failed every one there (failed even when it finished: ``_build`` re-checks the time); a board capture
now has ``max_capture_seconds`` (30 s), below the 60 s validity, so a slow capture still lands as fresh evidence.

Every ``ViewOwner`` uses this source, so the project tree's views (``fact_tree_startup``) are paced the same way;
the tree passes its own bounds (300 s validity, 60 s capture limit: its four-selector cycle outgrew 60 s). A capture
queued behind a wedged shared worker reports no failure: its evidence goes stale when its validity (60 s for a
board, 300 s for the tree) expires (``fact_expiry``) instead of within the owner's 10 s budget. The next refresh is
timed from the last evidence's observation, not its commit: one selector whose captures finish within 30 s keeps its
validity; in a round of several views, earlier views' evidence is older by the later captures' durations.
"""
from collections import deque
from contextvars import copy_context
from dataclasses import dataclass, replace
import hashlib
import json
import threading
import time

from hermes_cli.fact_capture import CaptureBudgetExceeded
from hermes_cli.fact_contract import Fact, FactKey, evolve, freeze
from hermes_cli.fact_store import view_key
from hermes_cli.fact_source_watch import NativeSourceWatch
from hermes_cli.fact_capture_queue import DEFAULT_LANE, submit


@dataclass(frozen=True)
class CapturedInput:
    value: object
    source_vector: tuple
    observed_at: float


@dataclass(frozen=True)
class BoardCapture:
    key: object = None
    value: object = None
    source_vector: tuple = ()
    observed_at: float = 0
    after: object = None
    next_deadline: float | None = None
    failed: bool = False


class BoardInputSource:
    """One bounded round-robin evidence source per authenticated board scope."""
    def __init__(self, engine, source, capture, *, paths=(), max_selectors=128, refresh_seconds=30, valid_seconds=60,
                 min_gap_seconds=5, max_capture_seconds=30, capture_lane=DEFAULT_LANE):
        if source.scope != engine.scope or source.kind != 'board.input':
            raise ValueError('invalid board input source')
        if (max_selectors <= 0 or not 0 < refresh_seconds < valid_seconds or not 0 <= min_gap_seconds < refresh_seconds
                or not 0 < max_capture_seconds < valid_seconds):
            raise ValueError('invalid board input bounds')
        self.engine, self.source, self._capture = engine, source, capture
        self.key = FactKey(source.scope, 'BoardInput', '')  # one operational input namespace per scope
        self._maximum, self._refresh, self._valid = max_selectors, refresh_seconds, valid_seconds
        self._lane = capture_lane  # fact_capture_queue lane: the tree captures on its own worker, not the boards'
        self._capture_seconds = max_capture_seconds  # replaces the owner budget's max_seconds; max_bytes still applies
        self._gap, self._last_start = min_gap_seconds, {}  # key -> monotonic start of its last capture
        self._began = None  # monotonic start of the running capture; its deadline (self._deadline) runs from here
        self._context = copy_context()
        self._lock = threading.Lock()
        self._wanted, self._pending = {}, deque()
        self._closed = False
        self._worker = self._result = self._wake = self._deadline = self._active = None
        self._paths, self._watch = tuple(paths), None

    def input_key(self, view, selector):
        return FactKey(self.source.scope, 'BoardInput', view_key(self.source.scope, view, selector).id)

    def want(self, view, selector):
        key = self.input_key(view, selector)
        with self._lock:
            if self._closed:
                return False
            if key not in self._wanted:
                if len(self._wanted) >= self._maximum:
                    return False
                self._wanted[key] = (view, freeze(selector))
                self._pending.append(key)
                wake = self._wake
            else:
                wake = None
        if wake is not None:
            wake()
        return True

    def watch(self, wake):
        with self._lock:
            if self._closed or self._wake is not None:
                raise RuntimeError('board source closed or already watched')
            self._wake = wake
        if self._paths:
            self._watch = NativeSourceWatch(self._paths, self._invalidate)
        return self

    def _invalidate(self):
        with self._lock:
            if self._closed:
                return
            pending = set(self._pending)
            self._pending.extend(key for key in self._wanted if key not in pending)
            wake = self._wake
        if wake is not None:
            wake()

    def capture(self, cursor, budget):
        if self._watch is not None:
            self._watch.check()
        with self._lock:
            if self._closed or self._wake is None:
                raise RuntimeError('board source unavailable')
            if self._result is not None:
                age = max(0.0, time.time() - (self._result.observed_at or time.time()))
                return replace(self._result, next_deadline=time.monotonic() + (
                    0 if self._pending else max(0.0, self._refresh - age)))
            if self._worker is None or not self._worker.is_alive():
                if not self._pending:
                    self._pending.extend(self._wanted)
                if not self._pending:
                    return BoardCapture(after=cursor)
                now = time.monotonic()
                key = self._next_due(now)
                if key is None:  # every pending selector ran within the gap: its writes wait to coalesce
                    return BoardCapture(after=cursor, next_deadline=min(
                        self._last_start[pending] + self._gap for pending in self._pending))
                self._active, self._last_start[key], self._began, self._deadline = key, now, None, None
                view, selector = self._wanted[key]
                self._worker = submit(lambda: self._context.run(self._build, key, view, selector, budget),
                                      lane=self._lane)
            if self._deadline is not None and time.monotonic() >= self._deadline:
                self._deadline = None
                return BoardCapture(key=self._active, after=cursor, failed=True, observed_at=time.time())
            if self._began is None:  # queued behind other jobs in this source's capture lane: no budget spent yet
                return BoardCapture(after=cursor, next_deadline=time.monotonic() + 1)
            return BoardCapture(after=cursor, next_deadline=self._deadline)

    def _next_due(self, now):
        """Pop the first pending selector whose last capture started at least the gap ago (caller holds the lock)."""
        for _ in range(len(self._pending)):
            key = self._pending.popleft()
            if now - self._last_start.get(key, float('-inf')) >= self._gap:
                return key
            self._pending.append(key)
        return None

    def _build(self, key, view, selector, budget):
        started = time.monotonic()
        with self._lock:
            self._began, self._deadline = started, started + self._capture_seconds
            self._last_start[key] = started  # the gap runs from the actual start, not from queueing
        try:
            captured = self._capture(view, selector, budget)
            encoded = json.dumps(captured.value, separators=(',', ':'), allow_nan=False).encode()
            if len(encoded) > budget.max_bytes or time.monotonic() - started > self._capture_seconds:
                raise CaptureBudgetExceeded('board evidence capture budget exceeded')
            result = BoardCapture(key, freeze(captured.value), captured.source_vector, captured.observed_at,
                                  hashlib.sha256(encoded).hexdigest())
        except Exception:  # health: allow BLE001 -- retain prior evidence stale; never publish exception text or empty success
            import logging
            logging.getLogger(__name__).debug('board capture failed view=%s', view, exc_info=True)
            result = BoardCapture(key=key, failed=True, observed_at=time.time())
        with self._lock:
            if self._closed:
                return
            # Disarm with the result: the queue marks the job done only after this returns, and a capture() in
            # between must not read this capture's deadline as still running (a stale wait, or a late failure).
            self._result, self._began, self._deadline = result, None, None
            wake = self._wake
        wake()

    def commit(self, capture):
        if capture.key is None:
            return
        if capture.failed:
            self._fail(capture.key)
        else:
            self.engine.publish([Fact(capture.key, capture.value, source_vector=capture.source_vector,
                observed_at=capture.observed_at, valid_until=capture.observed_at + self._valid, health='fresh')], now=time.time())
        with self._lock:
            # capture() supplies an updated scheduling deadline on a detached result.
            if self._result is not None and self._result.key == capture.key:
                self._result = None

    def _fail(self, key):
        old = self.engine.store.snapshot(self.engine.scope).get(key)
        fact = (evolve(old, health='stale', error_code='board_capture_failed')
                if old is not None and old.health != 'unknown'
                else Fact(key, {}, error_code='board_capture_failed'))
        self.engine.publish([fact], now=time.time())

    def failure(self):
        with self._lock:
            keys = tuple(self._wanted)
        for key in keys:
            self._fail(key)

    def close(self, timeout=5):
        with self._lock:
            self._closed = True
            worker = self._worker
        if self._watch is not None:
            self._watch.close()
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                raise TimeoutError('board capture did not stop; custody retained')
        with self._lock:
            self._wake = self._result = None
