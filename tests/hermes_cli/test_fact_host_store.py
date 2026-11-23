"""Host storage bounds and immutable-generation regressions."""
import pytest

from hermes_cli.fact_contract import Fact, FactKey, Position, Scope
from hermes_cli.fact_store import FactStore

SCOPE = Scope("install", "alpha")


def test_store_retained_generations_and_deletion():
    store = FactStore()
    keys = [FactKey(SCOPE, "TaskFact", str(i)) for i in range(1000)]
    store.publish(Fact(key, {"n": i}) for i, key in enumerate(keys))
    old = store.snapshot(SCOPE)
    store.publish([Fact(keys[1], {"n": 999})], deleted_keys=[keys[2]])
    assert old.get(keys[1]).value["n"] == 1
    assert old.get(keys[2]) is not None
    assert store.snapshot(SCOPE).get(keys[1]).value["n"] == 999
    assert store.snapshot(SCOPE).get(keys[2]) is None


def test_ring_accounts_payload_and_eviction_resets():
    store = FactStore(ring_bytes=1024)
    start = store.snapshot(SCOPE)
    store.publish([Fact(FactKey(SCOPE, "TaskFact", "big"), {"value": "x" * 2048})])
    delta = store.changes(SCOPE, Position(start.epoch, start.rev))
    assert delta.reset
    assert store._retained_bytes <= 1024


def test_invalid_subscriber_bounds_refused():
    with pytest.raises(ValueError):
        FactStore(subscriber_bytes=0)


def test_slow_subscriber_overflow_never_runs_callback_in_publish():
    store = FactStore(subscriber_envelopes=2, subscriber_bytes=1024)
    seen = []
    sub = store.subscribe(SCOPE, (), None, None, seen.append)
    for i in range(20):
        store.publish([Fact(FactKey(SCOPE, "TaskFact", "task"), {"n": i})])
    assert not seen
    assert sub.reset_pending and sub.queued_bytes == 0
    store.drain()
    assert len(seen) == 1 and seen[0].reset
    assert seen[0].upserts[0].value["n"] == 19
    sub.close()
