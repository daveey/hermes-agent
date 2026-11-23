"""In-process data plane and independently gated consumer release. [daveey]"""
import threading
import time

import pytest

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_store import FactStore


def scope(profile='default'):
    return Scope('gateway', profile, 'board')


def test_release_is_independent_and_requires_explicit_readiness():
    s = scope()
    life = IncrementalLifecycle({(s, 'annotations'), (s, 'board')})
    calls = []
    d = life.bind_consumer('annotations', s, lambda *args: calls.append(('d', *args)))
    e = life.bind_consumer('board', s, lambda *args: calls.append(('e', *args)))
    with pytest.raises(RuntimeError, match='not ready'):
        life.transition('annotations', s, 'facts', 1)
    life.report_ready('annotations', s)
    life.transition('annotations', s, 'facts', 1)
    assert calls == [('d', 'legacy', 0), ('e', 'legacy', 0), ('d', 'facts', 1)]
    life.transition('annotations', s, 'legacy', 2)
    d.close()
    e.close()
    assert not life.has_bindings(s)


def test_failed_release_rolls_back_only_its_consumer():
    s = scope()
    life = IncrementalLifecycle({(s, 'annotations'), (s, 'board')})
    calls = []

    def failed(mode, generation):
        calls.append((mode, generation))
        if mode == 'facts':
            raise ValueError('drain failed')

    life.bind_consumer('annotations', s, failed)
    other = []
    life.bind_consumer('board', s, lambda *args: other.append(args))
    life.report_ready('annotations', s)
    with pytest.raises(ExceptionGroup):
        life.transition('annotations', s, 'facts', 1)
    assert calls == [('legacy', 0), ('facts', 1), ('legacy', 2)]
    assert other == [('legacy', 0)]
    with pytest.raises(ValueError, match='unexpected consumer'):
        life.report_ready('board', scope('foreign'))


def test_local_reads_deliver_off_publisher_and_close_custody():
    s = scope()
    store = FactStore()
    life = IncrementalLifecycle({(s, 'board')})
    owner = InProcessFacts(store, {s}, demand=lambda *args: None)
    handle = owner.acquire(s, lifecycle=life)
    subscription = None
    try:
        observed = []
        delivered = threading.Event()
        publisher = threading.get_ident()
        key = FactKey(s, 'TaskFact', 't1')

        def receive(delta):
            if delta.upserts:
                observed.append((threading.get_ident(), delta))
                delivered.set()

        subscription = handle.v1.subscribe(s, ('TaskFact',), None, None, receive)
        store.publish([Fact(key, value={'title': 'first'})])
        assert delivered.wait(3)
        assert observed[-1][0] != publisher
        assert observed[-1][1].upserts[0].value['title'] == 'first'
        with pytest.raises(ValueError, match='unauthorized_scope'):
            handle.v1.snapshot(scope('foreign'))
        with pytest.raises(ValueError, match='unauthorized_scope'):
            owner.acquire(scope('foreign'), lifecycle=life)
        with pytest.raises(RuntimeError, match='handles'):
            owner.close()
    finally:
        if subscription:
            subscription.close()
        handle.close()
        owner.close()
    with pytest.raises(RuntimeError, match='closed'):
        handle.v1.snapshot(s)


def test_new_subscription_is_delivered_without_a_write():
    s = scope()
    store = FactStore()
    owner = InProcessFacts(store, {s}, demand=lambda *args: None)
    h = owner.acquire(s, lifecycle=IncrementalLifecycle({(s, 'board')}))
    ready = threading.Event()
    try:
        sub = h.v1.subscribe(s, (), None, None, lambda delta: ready.set())
        assert ready.wait(3)
        sub.close()
    finally:
        h.close()
        owner.close()


def test_local_expiry_and_failed_callback_retry_without_writes():
    s = scope()
    store = FactStore()
    owner = InProcessFacts(store, {s}, demand=lambda *args: None)
    h = owner.acquire(s, lifecycle=IncrementalLifecycle({(s, 'board')}))
    recovered = threading.Event()
    failures = []

    def receive(delta):
        if not failures:
            failures.append(True)
            raise ValueError('one failure')
        if any(f.health == 'stale' for f in delta.upserts):
            recovered.set()

    try:
        sub = h.v1.subscribe(s, (), None, None, receive)
        store.publish([Fact(FactKey(s, 'TaskFact', 't1'), value={}, health='fresh', valid_until=time.time() + .1)])
        assert recovered.wait(3)
        sub.close()
    finally:
        h.close()
        owner.close()


def test_demand_is_scoped_and_handle_close_does_not_stop_other_readers():
    s = scope()
    demands = []
    owner = InProcessFacts(FactStore(), {s}, demand=lambda *args: demands.append(args))
    life = IncrementalLifecycle({(s, 'board')})
    first = owner.acquire(s, lifecycle=life)
    second = owner.acquire(s, lifecycle=life)
    first.demand('board', {'archived': False})
    assert demands == [(s, 'board', {'archived': False})]
    first.close()
    assert second.v1.snapshot(s).rev == 0
    second.close()
    owner.close()
