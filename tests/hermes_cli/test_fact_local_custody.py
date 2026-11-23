"""Real concurrent local delivery, expiry and teardown custody. [daveey]"""
import threading
import time
from itertools import pairwise

import pytest

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_store import FactStore
from tests.waiting import wait_until


@pytest.fixture
def local():
    scope = Scope('install', 'profile')
    store = FactStore()
    owner = InProcessFacts(store, {scope}, demand=lambda *args: None)
    handle = owner.acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'board')}))
    yield scope, store, owner, handle
    handle.close()
    owner.close()


def test_close_waits_for_callback_and_expiry_does_not(local):
    scope, store, _owner, handle = local
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()

    def receive(_delta):
        entered.set()
        assert release.wait(3)

    handle.v1.subscribe(scope, (), None, None, receive)
    assert entered.wait(3)
    closer = threading.Thread(target=lambda: (handle.close(), closed.set()))
    try:
        key = FactKey(scope, 'TaskFact', 'deadline')
        store.publish([Fact(key, {}, health='fresh', valid_until=time.time() - 1)])
        assert wait_until(lambda: store.snapshot(scope).get(key).health == 'stale', timeout=2)
        closer.start()
        assert not closed.wait(.1)
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(3)
    assert closed.is_set()


def test_foreign_subscription_queue_not_consumed(local):
    scope, store, _owner, handle = local
    foreign = store.subscribe(Scope('install', 'other'), (), None, None, lambda _: None)
    delivered = threading.Event()
    try:
        own = handle.v1.subscribe(scope, (), None, None, lambda _: delivered.set())
        assert delivered.wait(3)
        assert foreign.reset_pending
        own.close()
    finally:
        foreign.close()


def test_subscription_churn_releases_callback_references(local):
    scope, _store, _owner, handle = local
    for _ in range(100):
        handle.v1.subscribe(scope, (), None, None, lambda _: None).close()
    assert not handle._replica.subscriptions


def test_callback_cannot_half_close_its_owner(local):
    scope, _store, owner, handle = local
    observed = []
    done = threading.Event()

    def receive(_delta):
        try:
            handle.close()
        except RuntimeError as exc:
            observed.append(str(exc))
        done.set()

    sub = handle.v1.subscribe(scope, (), None, None, receive)
    assert done.wait(3)
    assert observed and 'delivery thread' in observed[0]
    assert handle._replica in owner._readers
    sub.close()


def test_persistent_callback_failure_uses_backoff(local):
    scope, _store, _owner, handle = local
    calls = []
    first = threading.Event()

    def receive(_delta):
        calls.append(time.monotonic())
        first.set()
        raise ValueError('persistent failure')

    sub = handle.v1.subscribe(scope, (), None, None, receive)
    try:
        assert first.wait(3)
        assert wait_until(lambda: len(calls) >= 4, timeout=3)
        gaps = [b - a for a, b in pairwise(calls)]
        assert gaps[1] >= .18
        assert gaps[2] >= .38
    finally:
        sub.close()


def test_subscription_cancel_does_not_join_a_consumer_locked_callback(local):
    scope, _store, _owner, handle = local
    entered, release = threading.Event(), threading.Event()

    def receive(_delta):
        entered.set()
        assert release.wait(3)

    sub = handle.v1.subscribe(scope, (), None, None, receive)
    assert entered.wait(3)
    try:
        sub.close()  # Consumer transitions can hold a lock the callback needs.
        assert sub.subscription in handle._replica.retiring
    finally:
        release.set()


def test_contended_delivery_arms_retry_instead_of_spinning(local):
    scope, store, owner, handle = local
    delivered = threading.Event()
    sub = handle.v1.subscribe(scope, (), None, None, lambda _: delivered.set())
    assert delivered.wait(3)
    raw = sub.subscription
    with raw.delivery_lock:
        store.publish([Fact(FactKey(scope, 'TaskFact', 'contended'), {})])
        assert wait_until(lambda: raw in owner._retry, timeout=2)
        assert owner._retry[raw][0] > time.monotonic()
    assert wait_until(lambda: not raw.queue and not raw.reset_pending, timeout=2)
    sub.close()


def test_dead_worker_refuses_stale_reads_and_new_acquisitions(local):
    scope, _store, owner, handle = local
    with owner.store._condition:
        owner._stopping = True
        owner.store._condition.notify_all()
    owner._thread.join(3)
    with pytest.raises(RuntimeError, match='worker unavailable'):
        handle.v1.snapshot(scope)
    with pytest.raises(RuntimeError, match='worker unavailable'):
        owner.acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'board')}))
