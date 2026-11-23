"""RemoteLinksSource capture serializes with strict ``_links_lock`` readers. [daveey]

The source's canonical ``_project_remote_links`` shares ``_origin_rows_sink`` and the
ledger/replica reads with strict read-your-writes callers, which project under
``activity._links_lock``. The capture must take that same lock, count the wait against
its budget, and never start the expensive projection once the budget is spent waiting.
"""
import threading
import time

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore
from tests.waiting import wait_until


class _ObservedLock:
    """The real lock, plus an event set when a thread asks for it while another holds it."""

    def __init__(self):
        self._lock = threading.Lock()
        self.requested = threading.Event()
        self._holder = None

    def acquire(self, blocking=True, timeout=-1):
        if self._lock.locked() and threading.get_ident() != self._holder:
            self.requested.set()  # contended: another thread holds it
        got = self._lock.acquire(blocking, timeout)
        if got:
            self._holder = threading.get_ident()
        return got

    def release(self):
        self._holder = None
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def held_by_current_thread(self):
        return self._lock.locked() and self._holder == threading.get_ident()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()


@pytest.fixture
def links(monkeypatch):
    from hermes_cli.fact_remote_links import RemoteLinksSource
    lock = _ObservedLock()
    monkeypatch.setattr(activity, '_links_lock', lock)
    monkeypatch.setattr(activity, '_origin_scope', lambda: ('authorized',))
    calls = []

    def projector():
        calls.append(lock.held_by_current_thread())
        activity._origin_rows_sink.rows.append({'key': 'k'})
        return [{'key': 'k'}]

    monkeypatch.setattr(activity, '_project_remote_links', projector)
    scope = Scope('install', 'default')
    engine = ReducerEngine(FactStore(), scope)
    source = RemoteLinksSource(engine, SourceId(scope, 'remote.links', 'origin'), ('authorized',))
    woke = threading.Event()
    source.watch(woke.set)
    yield source, lock, calls, woke
    if lock.held_by_current_thread():
        lock.release()
    source.close()


def test_capture_projection_waits_for_strict_reader_and_holds_the_lock(links):
    source, lock, calls, woke = links
    lock.acquire()  # a strict reader is projecting under _links_lock
    source.capture(None, CaptureBudget(max_seconds=10))
    wait_until(lambda: calls or lock.requested.is_set(), what='capture projects or asks for _links_lock')
    assert calls == [], 'capture projected concurrently with a strict reader'
    released_at = time.time()
    lock.release()
    assert woke.wait(10)
    assert calls == [True], 'projection ran without holding _links_lock'
    result = source.capture(None, CaptureBudget(max_seconds=10))
    assert not result.failed
    assert result.value['cards'][0]['key'] == 'k' and result.value['origin_rows'][0]['key'] == 'k'
    assert result.observed_at >= released_at  # observed after the wait, not when the worker launched
    assert not lock.locked()
    source.commit(result)
    fact = source.engine.store.snapshot(source.engine.scope).get(source.key)
    assert fact.health == 'fresh'


def test_lock_wait_past_budget_fails_without_projecting(links):
    source, lock, calls, woke = links
    lock.acquire()  # held for the whole capture budget
    source.capture(None, CaptureBudget(max_seconds=0.05))
    assert woke.wait(10), 'lock-wait expiry never completed the capture'
    assert lock.requested.is_set(), 'capture never contended for _links_lock'
    assert calls == [], 'expired lock wait still ran the expensive projection'
    result = source.capture(None, CaptureBudget(max_seconds=0.05))
    assert result.failed and result.value is None
    source.commit(result)
    fact = source.engine.store.snapshot(source.engine.scope).get(source.key)
    assert fact.health == 'unknown' and fact.error_code == 'links_capture_time_budget'
