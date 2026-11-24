"""Expiry publication and ordinary publish both finish under main's lock order. [daveey]"""
import threading
import time

import pytest

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_expiry import ExpiryWorker
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
            expired = store.snapshot(scope).get(expires)
            assert expired is not None and expired.health == 'stale'
    finally:
        first.join(5)
        if second:
            second.join(5)
        if worker:
            worker.close()
