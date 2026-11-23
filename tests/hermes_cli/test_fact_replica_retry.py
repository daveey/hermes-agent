"""Quiet socket replicas retain callback retries without starving peers."""
import threading
from types import SimpleNamespace

from hermes_cli import fact_replica
from hermes_cli.fact_store import FactStore

import pytest

from hermes_cli.fact_contract import Scope
from hermes_cli.fact_host import FactHost, acquire
from hermes_cli.fact_replica import Endpoint
from tests.waiting import wait_until


@pytest.mark.platforms('linux')
def test_quiet_replica_retries_failed_callback_and_delivers_sibling(tmp_path_factory):
    scope = Scope('fixture', 'alpha')
    token = 'retry-fixture-capability-token-00000000'
    host = FactHost(tmp_path_factory.mktemp('retry'), {token: scope}).start()
    handle = acquire(scope, endpoint=Endpoint(str(host.directory / 'facts.sock'), token))
    recovered, sibling = threading.Event(), threading.Event()
    calls = []

    def transient(delta):
        calls.append(delta)
        if len(calls) == 1:
            raise RuntimeError('one transient consumer failure')
        recovered.set()

    try:
        assert wait_until(lambda: handle._online, timeout=5)
        revision = host.store.snapshot(scope).rev
        sub = handle.v1.subscribe(scope, (), None, None, transient)
        peer = handle.v1.subscribe(scope, (), None, None, lambda delta: sibling.set())
        assert sibling.wait(5)
        assert recovered.wait(3), 'quiet source must retry retained reset without publication'
        assert calls[-1].reset
        assert host.store.snapshot(scope).rev == revision
        sub.close()
        peer.close()
    finally:
        handle.close()
        host.close()
    assert not handle._callbacks.is_alive()


def test_retry_backoff_caps_and_stops_after_success(monkeypatch):
    scope = Scope('fixture', 'alpha')
    handle = object.__new__(fact_replica.ReplicaHandle)
    handle._store = FactStore()
    handle._stop = threading.Event()
    clock, waits, calls = [0.0], [], []
    monkeypatch.setattr(fact_replica, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    def wait(timeout=None):
        waits.append(timeout)
        if len(waits) > 10:
            pytest.fail('callback retry failed to become idle')
        if timeout is None and calls:
            handle._stop.set()
        elif timeout is not None:
            clock[0] += timeout

    handle._deliver = SimpleNamespace(wait=wait, clear=lambda: None)

    def callback(delta):
        calls.append(delta)
        if len(calls) <= 7:
            raise LookupError('consumer-specific exception is isolated too')

    handle._store.subscribe(scope, (), None, None, callback)
    handle._dispatch()
    assert waits[0] is None and waits[-1] is None
    assert waits[1:-1] == pytest.approx([.1, .2, .4, 1., 2., 5., 5.])
    assert len(calls) == 8
    assert all(delta.reset for delta in calls)


@pytest.mark.platforms('linux')
def test_failed_callback_close_cancels_retry(tmp_path_factory):
    scope = Scope('fixture', 'alpha')
    token = 'cancel-fixture-capability-token-0000000'
    host = FactHost(tmp_path_factory.mktemp('cancel'), {token: scope}).start()
    handle = acquire(scope, endpoint=Endpoint(str(host.directory / 'facts.sock'), token))
    failed = threading.Event()

    def broken(delta):
        failed.set()
        raise RuntimeError('persistent consumer failure')

    try:
        assert wait_until(lambda: handle._online, timeout=5)
        sub = handle.v1.subscribe(scope, (), None, None, broken)
        assert failed.wait(5)
        sub.close()
    finally:
        handle.close()
        host.close()
    assert not handle._callbacks.is_alive()
