"""Real process scheduler custody, profile context and eleven-profile census. [daveey]"""
from collections import Counter
from contextvars import ContextVar
from types import SimpleNamespace
import threading
import time

import pytest

from hermes_cli import fact_process as process
from hermes_cli.fact_board_inputs import CapturedInput
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Fact, FactKey, Scope, Ready
from hermes_cli.fact_view_owner import ViewOwner
from tests.waiting import wait_until


def test_eleven_authorized_profiles_one_used_thread_census(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def capture(view, selector, budget):
        entered.set()
        assert release.wait(5)
        return CapturedInput({'ok': True}, (), time.time())
    owners = [ViewOwner(Scope(str(tmp_path), f'p{i}'), capture, lambda view, value: dict(value),
                        ['board'], paths=[tmp_path / f'p{i}' / 'state.db']) for i in range(11)]
    baseline = Counter(t.name for t in threading.enumerate())
    assert len({id(owner.store) for owner in owners}) == 1
    assert Counter(t.name for t in threading.enumerate()) == baseline
    try:
        handle = owners[0].start()
        handle.demand('board', {})
        assert entered.wait(5)
        census = Counter(t.name for t in threading.enumerate())
        delta = {name: census[name] - baseline[name] for name in
                 ('fact-change-feed', 'fact-board-capture', 'fact-native-watch',
                  'fact-local-delivery', 'fact-expiry')}
        print('11 profiles / one consumer:', delta)
        assert delta['fact-change-feed'] == 1
        assert delta['fact-board-capture'] == 1
        assert delta['fact-native-watch'] <= 3
        assert delta['fact-local-delivery'] == delta['fact-expiry'] == 1
        assert all(owner.owner is None for owner in owners[1:])
        release.set()
        wait_until(lambda: isinstance(handle.v1.snapshot(owners[0].scope).view('board', {}), Ready))
    finally:
        release.set()
        for owner in owners:
            owner.close()
    wait_until(lambda: all(Counter(t.name for t in threading.enumerate())[name] == baseline[name]
                           for name in delta))


def test_leases_share_thread_preserve_budget_context_and_independent_close():
    profile = ContextVar('fact_process_profile', default='unbound')
    seen = []
    class Adapter:
        def watch(self, wake):
            return SimpleNamespace(close=lambda: None)
        def capture(self, cursor, budget):
            seen.append(('capture', profile.get(), budget.max_bytes))
            return SimpleNamespace(after=1)
    leases = []
    try:
        for name, limit in [('first', 1234), ('second', 2345)]:
            token = profile.set(name)
            try:
                lease = process.FeedLease(lambda adapter, captured: seen.append(('commit', profile.get())),
                                          lambda *args: pytest.fail('unexpected failure'),
                                          budget=CaptureBudget(max_bytes=limit))
                leases.append(lease)
                lease.register(Adapter())
            finally:
                profile.reset(token)
        assert leases[0]._thread is leases[1]._thread
        wait_until(lambda: len(seen) == 4)
        assert ('capture', 'first', 1234) in seen and ('capture', 'second', 2345) in seen
        assert ('commit', 'first') in seen and ('commit', 'second') in seen
        leases[0].close()
        assert leases[1]._thread.is_alive()
    finally:
        for lease in leases:
            lease.close()
    assert not leases[-1]._thread.is_alive()


def test_scope_retirement_does_not_erase_successor_or_sibling(tmp_path):
    storage = process.store()
    scope, sibling = Scope(str(tmp_path), 'p'), Scope(str(tmp_path), 'q')
    key, other = FactKey(scope, 'Input', 'x'), FactKey(sibling, 'Input', 'x')
    first = process.FeedLease(lambda *a: None, lambda *a: None, stores={scope: storage})
    second = process.FeedLease(lambda *a: None, lambda *a: None, stores={sibling: storage})
    successor = None
    try:
        storage.publish([Fact(key, 1), Fact(other, 2)])
        with pytest.raises(RuntimeError, match='scope custody'):
            process.FeedLease(lambda *a: None, lambda *a: None, stores={scope: storage})
        first.close()
        assert storage.snapshot(scope).get(key) is None
        assert storage.snapshot(sibling).get(other).value == 2
        successor = process.FeedLease(lambda *a: None, lambda *a: None, stores={scope: storage})
        storage.publish([Fact(key, 3)])
        first.close()
        assert storage.snapshot(scope).get(key).value == 3
    finally:
        first.close()
        second.close()
        if successor is not None:
            successor.close()


def test_activation_coalesces_active_work_and_restarts_after_idle():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def work():
        calls.append('first')
        entered.set()
        assert release.wait(5)
    try:
        process.submit(('test', id(entered)), work)
        assert entered.wait(5)
        process.submit(('test', id(entered)), lambda: pytest.fail('duplicate activation'))
        release.set()
        wait_until(lambda: process._worker is None)
        process.submit(('test', id(entered)), lambda: (calls.append('second'), finished.set()))
        assert finished.wait(5)
        assert calls == ['first', 'second']
    finally:
        release.set()
        wait_until(lambda: process._worker is None)
