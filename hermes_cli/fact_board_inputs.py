"""Demanded native board/tree evidence on the existing ContentOwner feed. [daveey]

This adapter owns no task/session clock or route response cache. Trusted startup
supplies scoped native capture functions; the engine's ordinary view reducers
consume their detached inputs. Request demand only edits a bounded memory queue.
Each worker turn captures one selector, so a slow view cannot starve its peers.
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
from hermes_cli.fact_capture_queue import submit


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
    def __init__(self, engine, source, capture, *, paths=(), max_selectors=128, refresh_seconds=30, valid_seconds=60):
        if source.scope != engine.scope or source.kind != 'board.input':
            raise ValueError('invalid board input source')
        if max_selectors <= 0 or not 0 < refresh_seconds < valid_seconds:
            raise ValueError('invalid board input bounds')
        self.engine, self.source, self._capture = engine, source, capture
        self.key = FactKey(source.scope, 'BoardInput', '')  # one operational input namespace per scope
        self._maximum, self._refresh, self._valid = max_selectors, refresh_seconds, valid_seconds
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
                return replace(self._result, next_deadline=time.monotonic() + (0 if self._pending else self._refresh))
            if self._worker is None or not self._worker.is_alive():
                if not self._pending:
                    self._pending.extend(self._wanted)
                if not self._pending:
                    return BoardCapture(after=cursor)
                self._active = self._pending.popleft()
                self._deadline = time.monotonic() + budget.max_seconds
                view, selector = self._wanted[self._active]
                key = self._active
                self._worker = submit(lambda: self._context.run(self._build, key, view, selector, budget))
            if self._deadline is not None and time.monotonic() >= self._deadline:
                self._deadline = None
                return BoardCapture(key=self._active, after=cursor, failed=True, observed_at=time.time())
            return BoardCapture(after=cursor, next_deadline=self._deadline)

    def _build(self, key, view, selector, budget):
        started = time.monotonic()
        try:
            captured = self._capture(view, selector, budget)
            encoded = json.dumps(captured.value, separators=(',', ':'), allow_nan=False).encode()
            if len(encoded) > budget.max_bytes or time.monotonic() - started > budget.max_seconds:
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
            self._result = result
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
        fact = (evolve(old, health='stale', error_code='board_capture_failed') if old is not None
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
