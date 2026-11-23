"""Origin-link display capture for the in-process feed. [daveey]

Trusted startup constructs one source per origin. Expensive canonical ledger /
replica reads run with that captured context, never on the feed or request thread.
A single retained result is committed by the feed, with a 30s recapture deadline.
This bounded-refresh external adapter does NOT replace strict read-your-writes
callers, authorize a request, drain the legacy refresher, or activate serve.
"""
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, replace
import hashlib
import json
import threading
import time

from hermes_cli.fact_capture import CaptureBudget, CaptureBudgetExceeded
from hermes_cli.fact_contract import Fact, FactKey, freeze

_current = ContextVar('remote_links_facts', default=None)


@dataclass(frozen=True)
class LinksBudget(CaptureBudget):
    """Bound the complete external inventory, independently of task-page limits.

    Links include retained legacy associations, not merely open tasks. Keep
    explicit row/byte/time ceilings and never publish a truncated generation.
    """
    max_keys: int = 16384
    max_seconds: float = 15
    max_bytes: int = 16 * 1024 * 1024

    def __post_init__(self):
        if not (1 <= self.max_keys <= 16384 and 0 < self.max_seconds <= 15
                and 1 <= self.max_bytes <= 16 * 1024 * 1024):
            raise ValueError('invalid links capture budget')


class LinksUnavailable(RuntimeError):
    """A participating display scope has no current projection; never scan."""

    def __init__(self, message, *, error_code='links_unavailable'):
        super().__init__(message)
        self.error_code = error_code


@contextmanager
def use(binding):
    """Request middleware pins an already authorized binding; None is legacy."""
    token = _current.set(binding)
    try:
        yield
    finally:
        _current.reset(token)


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class LinksFacts:
    def __init__(self, handle, scope, origin_scope):
        self.handle, self.scope = handle, scope
        self.origin_scope = origin_scope
        self.key = FactKey(scope, 'RemoteLinksInput', 'origin')

    def read(self, with_rows):
        from hermes_cli import kanban_session_activity_revalidate as revalidate
        if revalidate.forced(self.origin_scope, 'links'):
            raise LinksUnavailable('forced links revalidation requires strict capture')
        try:
            fact = self.handle.v1.snapshot(self.scope).get(self.key)
        except (ValueError, RuntimeError) as exc:
            raise LinksUnavailable('remote links owner unavailable') from exc
        if (fact is None or fact.health != 'fresh' or fact.valid_until is None
                or fact.valid_until <= time.time()):
            raise LinksUnavailable('remote links projection unavailable',
                                   error_code=(fact.error_code or 'links_capture_expired') if fact is not None else 'links_capture_pending')
        cards = _thaw(fact.value['cards'])
        return (cards, _thaw(fact.value['origin_rows'])) if with_rows else cards


@dataclass(frozen=True)
class LinksCapture:
    value: object = None
    after: object = None
    observed_at: float = 0
    next_deadline: float | None = None
    failed: bool = False
    error_code: str = 'links_capture_failed'


class RemoteLinksSource:
    def __init__(self, engine, source, origin_scope):
        if source.scope != engine.scope or source.kind != 'remote.links':
            raise ValueError('invalid remote links source')
        if 'RemoteLinksInput' in engine._specs:
            raise ValueError('remote links input cannot be derived')
        self.engine, self.source = engine, source
        self.key = FactKey(engine.scope, 'RemoteLinksInput', 'origin')
        self._origin_scope = origin_scope
        self._context = copy_context()
        self._lock = threading.Lock()
        self._worker = self._wake = self._result = None
        self._deadline = None
        self._closed = False

    def watch(self, wake):
        with self._lock:
            if self._closed or self._wake is not None:
                raise RuntimeError('remote links source closed or already watched')
            self._wake = wake
        return self

    def capture(self, cursor, budget):
        with self._lock:
            if self._closed or self._wake is None:
                raise RuntimeError('remote links source unavailable')
            if self._result is not None:
                return self._result
            if self._worker is None or not self._worker.is_alive():
                self._deadline = time.monotonic() + budget.max_seconds
                self._worker = threading.Thread(target=self._context.run, args=(self._build, budget),
                                                daemon=True, name='fact-links-capture')
                self._worker.start()
            if self._deadline is not None and time.monotonic() >= self._deadline:
                self._deadline = None
                return LinksCapture(after=cursor, failed=True, observed_at=time.time(),
                                    error_code='links_capture_time_budget')
            # One deadline marks a hung capture unavailable; only completion
            # wakes it afterwards. Never launch a replacement alongside it.
            return LinksCapture(after=cursor, next_deadline=self._deadline)

    def _build(self, budget):
        from hermes_cli import kanban_session_activity as activity
        started = time.monotonic()
        error_code = 'links_capture_failed'
        try:
            if activity._origin_scope() != self._origin_scope:
                error_code = 'links_capture_scope_changed'
                raise ValueError('remote links capture scope changed')
            # Strict readers project under _links_lock and share its row sink and board reads: serialize
            # with them. The wait spends this capture's budget; once spent, never start the projection.
            remaining = budget.max_seconds - (time.monotonic() - started)
            if remaining <= 0 or not activity._links_lock.acquire(timeout=remaining):
                error_code = 'links_capture_time_budget'
                raise CaptureBudgetExceeded('remote links capture budget exceeded waiting for the projection')
            try:
                observed_at = time.time()  # the projection observes stores from here, not from launch
                activity._origin_rows_sink.rows = []
                try:
                    cards = activity._project_remote_links()
                    rows = activity._origin_rows_sink.rows
                finally:
                    activity._origin_rows_sink.rows = None
            finally:
                activity._links_lock.release()
            value = {'cards': cards, 'origin_rows': rows}
            encoded = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
            if len(cards) + len(rows) > budget.max_keys:
                error_code = 'links_capture_row_budget'
            elif len(encoded) > budget.max_bytes:
                error_code = 'links_capture_byte_budget'
            elif time.monotonic() - started > budget.max_seconds:
                error_code = 'links_capture_time_budget'
            if error_code != 'links_capture_failed':
                raise CaptureBudgetExceeded('remote links capture budget exceeded')
            result = LinksCapture(freeze(value), hashlib.sha256(encoded).hexdigest(), observed_at)
        except Exception:  # health: allow BLE001 -- source failure publishes stale without exception text or empty success
            result = LinksCapture(failed=True, observed_at=time.time(), error_code=error_code)
        with self._lock:
            if self._closed:
                return
            self._result = replace(result, next_deadline=time.monotonic() + 30)
            wake = self._wake
        wake()

    def commit(self, capture):
        if capture.value is None and not capture.failed:
            return
        if capture.failed:
            self.failure(capture.error_code)
        else:
            self.engine.publish([Fact(self.key, capture.value, source_vector=((self.source, capture.after),),
                                      observed_at=capture.observed_at, valid_until=capture.observed_at + 90,
                                      health='fresh')], now=time.time())
        with self._lock:
            if self._result is capture:
                self._result = None

    def failure(self, error_code='links_capture_failed'):
        old = self.engine.store.snapshot(self.engine.scope).get(self.key)
        fact = (replace(old, health='stale', error_code=error_code) if old is not None
                and old.health != 'unknown' else Fact(self.key, {}, error_code=error_code))
        self.engine.publish([fact], now=time.time())

    def close(self, timeout=5):
        with self._lock:
            self._closed = True
            worker = self._worker
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                raise TimeoutError('links capture did not stop; owner custody retained')
        with self._lock:
            self._wake = self._result = None
