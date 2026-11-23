"""Shared projection snapshots must be immutable and scope isolated."""

import pytest


def test_atomic_scoped_snapshot_retains_its_generation():
    from hermes_cli.fact_contract import Fact, FactKey, Scope
    from hermes_cli.fact_store import FactStore

    scope = Scope("install", "default", "board")
    other = Scope("install", "other", "board")
    key = FactKey(scope, "TaskFact", "authority/task")
    foreign = FactKey(other, "TaskFact", "authority/task")
    value = {"status": "ready", "parents": ["a"]}
    store = FactStore()
    store.publish((Fact(key, value), Fact(foreign, {"status": "private"})))
    before = store.snapshot(scope)
    value["parents"].append("b")
    assert before.get(key).value["parents"] == ("a",)
    with pytest.raises(TypeError):
        before.get(key).value["status"] = "mutated"
    assert before.get(foreign) is None
    assert before.get_many((key, foreign)) == (before.get(key), None)
    store.publish((Fact(key, {"status": "done"}),), deleted_keys=(foreign,))
    after = store.snapshot(scope)
    assert after.rev == before.rev + 1
    assert before.get(key).value["status"] == "ready"
    assert after.get(key).value["status"] == "done"
    store.publish((Fact(key, {"status": "done"}),))
    assert store.snapshot(scope).rev == after.rev


def test_view_lookup_and_ring_scoped_reset():
    from hermes_cli.fact_contract import Fact, FactKey, Pending, Position, Ready, Scope, Unavailable
    from hermes_cli.fact_store import FactStore

    scope = Scope("i", "p")
    store = FactStore(ring_revisions=2)
    assert isinstance(store.snapshot(scope).view("sessions", {"limit": 10}), Pending)
    key = store.view_key(scope, "sessions", {"limit": 10, "full": False})
    fact = Fact(key, {"encoded": b'{"sessions":[]}'}, health="fresh")
    store.publish((fact,))
    first = store.snapshot(scope)
    assert first.view("sessions", {"full": False, "limit": 10}) == Ready(
        b'{"sessions":[]}', first.epoch, first.rev)
    store.publish((Fact(key, fact.value, health="stale", error_code="source_offline"),))
    assert store.snapshot(scope).view("sessions", {"limit": 10, "full": False}) == Unavailable("source_offline")
    store.publish(deleted_keys=(key,))
    delta = store.changes(scope, Position(first.epoch, first.rev))
    assert delta.deletes == (key,)
    assert not delta.reset
    assert store.changes(scope, Position("old-owner", 1)).reset
    assert store.changes(scope, Position(first.epoch, 100)).reset
    assert store.changes(scope, Position(first.epoch, 0)).reset
    assert store.changes(Scope("foreign", "p"), None).upserts == ()
    store.publish((Fact(FactKey(scope, "TaskFact", "t"), {"status": "ready"}),))
    assert store.changes(scope, Position(first.epoch, first.rev)).reset


def test_subscriber_catchup_overflow_and_reentrant_callbacks():
    from hermes_cli.fact_contract import Fact, FactKey, Scope
    from hermes_cli.fact_fake import FakeStore

    store = FakeStore(subscriber_envelopes=2)
    scope = Scope("i", "p")
    key = FactKey(scope, "TaskFact", "t")
    received = []

    def callback(delta):
        received.append(delta)
        assert store.snapshot(scope).rev >= delta.rev

    sub = store.subscribe(scope, ("TaskFact",), (key,), None, callback)
    for value in range(5):
        store.publish((Fact(key, {"value": value}),))
    assert received == []  # producer never invokes a potentially slow callback
    store.drain()
    assert received[-1].reset
    assert received[-1].upserts[0].value["value"] == 4
    position = store.position(received[-1].epoch, received[-1].rev)
    sub.close()
    store.publish(deleted_keys=(key,))
    seen = []
    store.subscribe(scope, ("TaskFact",), (key,), position, seen.append)
    store.drain()
    assert seen[-1].deletes == (key,)
    assert received[-1].rev == position.rev


def test_readers_never_observe_a_torn_generation():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from hermes_cli.fact_contract import Fact, FactKey, Scope
    from hermes_cli.fact_store import FactStore

    scope = Scope("i", "p")
    keys = tuple(FactKey(scope, "TaskFact", str(index)) for index in range(4))
    store = FactStore()
    store.publish(tuple(Fact(key, 0) for key in keys))
    boundary = Barrier(2)

    def writer():
        for generation in range(1, 51):
            boundary.wait(timeout=5)
            store.publish(tuple(Fact(key, generation) for key in keys))
            boundary.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(writer)
        for _generation in range(50):
            boundary.wait(timeout=5)
            snapshot = store.snapshot(scope)
            assert len({fact.value for fact in snapshot.get_many(keys)}) == 1
            boundary.wait(timeout=5)
        result.result(timeout=5)
    assert {fact.value for fact in store.snapshot(scope).get_many(keys)} == {50}
