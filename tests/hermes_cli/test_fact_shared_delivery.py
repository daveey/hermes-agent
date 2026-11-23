"""Lazy store-wide workers retain per-transport reader custody."""
import threading
import time
from contextvars import ContextVar

import pytest

from tests.waiting import wait_until

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_store import FactStore


def test_construction_is_inert_and_eleven_transports_share_workers():
    scope = Scope('install', 'profile')
    store = FactStore()
    before = set(threading.enumerate())
    owners = [InProcessFacts(store, {scope}, demand=lambda *args: None) for _ in range(11)]
    handle = None
    try:
        assert set(threading.enumerate()) == before
        handle = owners[0].acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'board')}))
        created = set(threading.enumerate()) - before
        assert sorted(t.name for t in created) == ['fact-expiry', 'fact-local-delivery']
        handle.close()
        handle = None
        owners[0].close()
        assert all(not thread.is_alive() for thread in created)
    finally:
        if handle is not None:
            handle.close()
        for owner in owners:
            owner.close()


def test_active_transports_share_workers_but_keep_demands_and_close_independent():
    store = FactStore()
    scopes = [Scope('install', name) for name in ('first', 'second')]
    demands = [[], []]
    owners = [InProcessFacts(store, {scope}, demand=lambda *args, i=i: demands[i].append(args))
              for i, scope in enumerate(scopes)]
    handles = [owner.acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'board')}))
               for owner, scope in zip(owners, scopes)]
    delivered = [threading.Event(), threading.Event()]
    try:
        assert owners[0]._thread is owners[1]._thread
        assert owners[0]._expiry is owners[1]._expiry
        for i, handle in enumerate(handles):
            handle.demand('board', {'owner': i})
            handle.v1.subscribe(scopes[i], (), None, None, lambda _, i=i: delivered[i].set())
        assert all(event.wait(3) for event in delivered)
        assert demands == [[(scopes[i], 'board', {'owner': i})] for i in range(2)]
        handles[0].close()
        owners[0].close()
        assert owners[1]._thread.is_alive()
        assert owners[1]._expiry.thread.is_alive()
        delivered[1].clear()
        store.publish([Fact(FactKey(scopes[1], 'TaskFact', 'after-close'), {})])
        assert delivered[1].wait(3)
    finally:
        for handle in handles:
            handle.close()
        for owner in owners:
            owner.close()
    assert not owners[1]._thread.is_alive()
    assert not owners[1]._expiry.thread.is_alive()


def test_expiry_and_unrelated_close_continue_while_shared_callback_is_blocked():
    scope = Scope('install', 'profile')
    store = FactStore()
    owners = [InProcessFacts(store, {scope}, demand=lambda *args: None) for _ in range(2)]
    handles = [owner.acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'board')}))
               for owner in owners]
    entered, release = threading.Event(), threading.Event()

    def blocked(_):
        entered.set()
        assert release.wait(5)

    try:
        handles[1].v1.subscribe(scope, (), None, None, blocked)
        assert entered.wait(3)
        handles[0].close()
        owners[0].close(timeout=0)
        key = FactKey(scope, 'TaskFact', 'expiry')
        store.publish([Fact(key, {}, health='fresh', valid_until=time.time() - 1)])
        assert wait_until(lambda: store.snapshot(scope).get(key).health == 'stale', timeout=3)
        assert owners[1]._thread.is_alive()
    finally:
        release.set()
        for handle in handles:
            handle.close()
        for owner in owners:
            owner.close()


def test_shared_callbacks_keep_transport_context():
    profile = ContextVar('shared-delivery-test-profile', default='unset')
    scope = Scope('install', 'profile')
    store = FactStore()
    owners, handles = [], []
    observed = []
    done = [threading.Event(), threading.Event()]
    try:
        for i in range(2):
            token = profile.set(str(i))
            try:
                owners.append(InProcessFacts(store, {scope}, demand=lambda *args: None))
            finally:
                profile.reset(token)
            handles.append(owners[-1].acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'board')})))

            def receive(_, i=i):
                observed.append((i, profile.get()))
                done[i].set()

            token = profile.set(str(i))
            try:
                handles[-1].v1.subscribe(scope, (), None, None, receive)
            finally:
                profile.reset(token)
        assert all(event.wait(3) for event in done)
        assert sorted(observed) == [(0, '0'), (1, '1')]
    finally:
        for handle in handles:
            handle.close()
        for owner in owners:
            owner.close()


def test_failed_last_worker_join_retains_store_custody(monkeypatch):
    scope = Scope('install', 'profile')
    store = FactStore()
    owner = InProcessFacts(store, {scope}, demand=lambda *args: None)
    handle = owner.acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'board')}))
    handle.close()
    workers = store._local_delivery
    dormant = InProcessFacts(store, {scope}, demand=lambda *args: None)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner._thread, 'join', lambda timeout: None)
            patch.setattr(owner._thread, 'is_alive', lambda: True)
            with pytest.raises(TimeoutError, match='custody retained'):
                owner.close(timeout=0)
            assert store._local_delivery is workers
            assert owner in workers.owners
            with pytest.raises(RuntimeError, match='worker unavailable'):
                dormant.acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'board')}))
        owner.close()
        assert store._local_delivery is None
    finally:
        owner.close()
        dormant.close()
