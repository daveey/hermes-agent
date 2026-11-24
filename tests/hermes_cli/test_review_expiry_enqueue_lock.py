"""Expiry publication and ordinary publish both finish under main's lock order. [daveey]"""
import threading
import time

import pytest

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_expiry import ExpiryIndex, ExpiryWorker
from hermes_cli.fact_store import FactStore


@pytest.mark.parametrize('expiry', [False, True])
def test_expiry_and_offlock_enqueue_make_progress(monkeypatch, expiry):
    store = FactStore()
    scope = Scope('review', 'default')
    expires = FactKey(scope, 'TaskFact', 'expires')
    key = FactKey(scope, 'TaskFact', 'other')
    store.publish([Fact(expires, {'v': 0}, health='fresh', valid_until=time.time() - 1)])
    store.subscribe(scope, (), None, None, lambda delta: None)
    store.drain()
    first = threading.Thread(name='review-publisher', target=lambda: store.publish([Fact(key, {'v': 1})]))
    first.start()
    worker = None
    second = None
    expiry_handled = threading.Event()
    original_expire = ExpiryIndex.expire

    def expire(index, owner, now):
        original_expire(index, owner, now)
        if owner is store and owner._facts[expires].health == 'stale':
            expiry_handled.set()

    monkeypatch.setattr(ExpiryIndex, 'expire', expire)
    try:
        if expiry:
            worker = ExpiryWorker(store)
        else:
            second = threading.Thread(target=lambda: store.publish([Fact(key, {'v': 2})]))
            second.start()
        first.join(4)
        if second:
            second.join(4)
        assert not first.is_alive()
        if second:
            assert not second.is_alive()
        if expiry:
            # A request may observe the pre-expiry state before the worker runs.
            store.snapshot(scope)
            assert expiry_handled.wait(4), 'expiry worker did not publish stale health'
            expired = store.snapshot(scope).get(expires)
            assert expired is not None and expired.health == 'stale'
    finally:
        first.join(5)
        if second:
            second.join(5)
        if worker:
            worker.close()


def test_expiry_progress_waits_for_worker_scheduling(monkeypatch):
    """The publisher can finish before the expiry thread is scheduled."""
    release = threading.Event()
    armed = threading.Event()
    original_init = ExpiryWorker.__init__
    original_snapshot = FactStore.snapshot
    original_run = ExpiryWorker.run
    original_close = ExpiryWorker.close

    def init(self, store):
        armed.set()
        original_init(self, store)

    def snapshot(self, *args, **kwargs):
        result = original_snapshot(self, *args, **kwargs)
        if armed.is_set():
            release.set()
        return result

    def delayed_run(self):
        assert release.wait(5)
        original_run(self)

    def close(self):
        release.set()
        original_close(self)

    monkeypatch.setattr(ExpiryWorker, '__init__', init)
    monkeypatch.setattr(FactStore, 'snapshot', snapshot)
    monkeypatch.setattr(ExpiryWorker, 'run', delayed_run)
    monkeypatch.setattr(ExpiryWorker, 'close', close)
    test_expiry_and_offlock_enqueue_make_progress(monkeypatch, True)
