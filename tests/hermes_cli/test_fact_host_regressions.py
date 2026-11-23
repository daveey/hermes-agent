"""Host admission and callback recovery boundaries."""
import threading
import time

import pytest

from hermes_cli.fact_contract import Delta, Fact, FactKey, Scope
from hermes_cli.fact_expiry import ExpiryWorker
from hermes_cli.fact_replica import ReplicaHandle
from hermes_cli.fact_store import FactStore
from tests.waiting import wait_until

SCOPE = Scope("install", "profile")
KEY = FactKey(SCOPE, "TaskFact", "one")


def test_reconnect_empty_delta_restores_callback_health():
    # Drive the reader's actual apply boundary without a competing socket thread.
    handle = object.__new__(ReplicaHandle)
    handle.scope = SCOPE
    handle._store = FactStore()
    handle._online = False
    handle._deliver = threading.Event()
    first = Delta("term", 0, 1, (Fact(KEY, {}, health="fresh"),), (), True)
    handle._apply(first)
    seen = []
    sub = handle._store.subscribe(SCOPE, (), None, None, seen.append)
    handle._store.drain()
    seen.clear()
    handle._online = False
    handle._apply(Delta("term", 1, 1, (), (), False))
    handle._store.drain()
    assert len(seen) == 1 and seen[0].reset
    assert seen[0].upserts[0].health == "fresh"
    sub.close()


def test_far_future_deadline_cannot_kill_expiry():
    store = FactStore()
    store.publish([Fact(KEY, {}, health="fresh", valid_until=1e20)])
    worker = ExpiryWorker(store)
    try:
        # Publication wakes the same worker to process a newly earlier deadline.
        store.publish([Fact(KEY, {"new": True}, health="fresh", valid_until=time.time())])
        assert wait_until(lambda: store.snapshot(SCOPE).get(KEY).health == "stale", timeout=5)
        assert worker.thread.is_alive()
    finally:
        worker.close()


def test_generation_budget_refuses_atomically_and_recovers_after_delete():
    store = FactStore(fact_bytes=4096)
    store.publish([Fact(KEY, {"data": "x" * 1500})])
    before = store.snapshot(SCOPE)
    other = FactKey(SCOPE, "TaskFact", "two")
    with pytest.raises(ValueError, match="fact_budget_exceeded"):
        store.publish([Fact(other, {"data": "y" * 3000})])
    assert store.snapshot(SCOPE).rev == before.rev
    assert store.snapshot(SCOPE).get(other) is None
    store.publish([Fact(other, {"data": "y" * 1500})], deleted_keys=[KEY])
    assert store.snapshot(SCOPE).get(KEY) is None
    assert store.snapshot(SCOPE).get(other) is not None
