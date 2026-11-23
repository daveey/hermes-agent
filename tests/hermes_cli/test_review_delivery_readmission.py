"""A refused acquire must not bind a dormant transport to retired workers."""
import threading
import pytest
from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_store import FactStore
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle


def test_acquire_retries_after_other_transport_finishes_draining(monkeypatch):
    scope = Scope('review-fixture', 'p')
    store = FactStore()
    first = InProcessFacts(store, {scope}, demand=lambda *a: None)
    dormant = InProcessFacts(store, {scope}, demand=lambda *a: None)
    life = lambda: IncrementalLifecycle({(scope, 'board')})
    handle = first.acquire(scope, lifecycle=life())
    handle.close()
    entered, release = threading.Event(), threading.Event()
    original = first._expiry.close
    errors = []
    successor = None

    def delayed_close():
        entered.set()
        assert release.wait(5)
        original()

    def close():
        try:
            first.close()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(first._expiry, 'close', delayed_close)
    thread = threading.Thread(target=close)
    try:
        thread.start()
        assert entered.wait(3)
        # Refusal while the old worker has custody is correct.
        with pytest.raises(RuntimeError, match='worker unavailable'):
            dormant.acquire(scope, lifecycle=life())
        assert dormant._workers is None
        assert dormant not in first._workers.owners
        assert store._local_delivery is first._workers
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        assert not errors
        # Same consumer retries after a fully successful old-owner drain.
        successor = dormant.acquire(scope, lifecycle=life())
        assert dormant._thread.is_alive()
        assert dormant._workers is not first._workers
        delivered = threading.Event()
        observed = []

        def receive(delta):
            if delta.upserts:
                observed.extend(delta.upserts)
                delivered.set()

        sub = successor.v1.subscribe(scope, ('TaskFact',), None, None, receive)
        try:
            store.publish([Fact(FactKey(scope, 'TaskFact', 'successor'), value={'title': 'live'})])
            assert delivered.wait(3)
            assert observed[-1].value['title'] == 'live'
        finally:
            sub.close()
    finally:
        release.set()
        thread.join(3)
        if successor is not None:
            successor.close()
        first.close()
        dormant.close()
