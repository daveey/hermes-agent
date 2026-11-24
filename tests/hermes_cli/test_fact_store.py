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


def test_unchanged_upserts_are_not_reencoded_but_changes_are(monkeypatch):
    """2026-11-23: the reducers republish every dirty derived fact unchanged and the store JSON-encoded each one
    to size it, though the size is only used for a change (45% of the hub's fact-change-feed thread)."""
    from hermes_cli import fact_store as store_mod
    from hermes_cli.fact_contract import Fact, FactKey, Scope

    scope = Scope("install", "default", "board")
    big, small = FactKey(scope, "ViewFact", "big"), FactKey(scope, "TaskFact", "small")
    store = store_mod.FactStore()
    encoded = []
    real = store_mod.encoded_size
    monkeypatch.setattr(store_mod, "encoded_size", lambda fact: encoded.append(fact.key) or real(fact))
    first = store.publish((Fact(big, {"rows": list(range(500))}), Fact(small, {"status": "ready"})))
    assert sorted(k.id for k in encoded) == ["big", "small"]
    encoded.clear()
    # same content again (as a reducer hands back an unchanged derived fact): no encode, no new revision
    assert store.publish((Fact(big, {"rows": list(range(500))}), Fact(small, {"status": "ready"}))) == first
    assert encoded == []
    # one changed, one unchanged: only the change is encoded and committed
    second = store.publish((Fact(big, {"rows": list(range(500))}), Fact(small, {"status": "done"})))
    assert encoded == [small] and second == first + 1
    assert store.snapshot(scope).get(small).value["status"] == "done"


def test_unchanged_skip_keeps_byte_budget_and_wire_validation():
    from hermes_cli.fact_contract import Fact, FactKey, Scope
    from hermes_cli.fact_store import FactStore

    scope = Scope("install", "default", "board")
    key = FactKey(scope, "ViewFact", "v")
    store = FactStore(fact_bytes=4096)
    store.publish((Fact(key, {"text": "x" * 100}),))
    before = store._fact_bytes
    store.publish((Fact(key, {"text": "x" * 100}),))  # unchanged: budget untouched
    assert store._fact_bytes == before
    with pytest.raises(ValueError, match="fact_budget_exceeded"):  # a change is still sized and budgeted
        store.publish((Fact(key, {"text": "x" * 8000}),))
    with pytest.raises(TypeError):  # a new value that cannot cross the wire is still refused
        store.publish((Fact(FactKey(scope, "ViewFact", "bad"), {"obj": object()}),))


def test_a_fact_that_changes_after_the_unchanged_check_is_sized_on_retry(monkeypatch):
    """The equality check runs outside the publication lock; a concurrent publish can change the stored fact
    before the commit. The commit then refuses the unsized fact and publish sizes it and retries."""
    from hermes_cli import fact_store as store_mod
    from hermes_cli.fact_contract import Fact, FactKey, Scope

    scope = Scope("install", "default", "board")
    key = FactKey(scope, "TaskFact", "t")
    store = store_mod.FactStore()
    store.publish((Fact(key, {"status": "ready"}),))
    real_publish = store._publish
    raced = []

    def racing(prepared, deleted_keys):
        if not raced:  # another writer lands between the check and this commit
            raced.append(True)
            real_publish([(Fact(key, {"status": "running"}), 200)], ())
        return real_publish(prepared, deleted_keys)
    monkeypatch.setattr(store, "_publish", racing)
    encoded = []
    real = store_mod.encoded_size
    monkeypatch.setattr(store_mod, "encoded_size", lambda fact: encoded.append(fact.key) or real(fact))
    rev = store.publish((Fact(key, {"status": "ready"}),))  # equal to the store at check time
    assert encoded == [key], "the raced fact was committed without being sized"
    assert store.snapshot(scope).get(key).value["status"] == "ready" and rev == store.snapshot(scope).rev


@pytest.mark.parametrize("case", ["delete_then_same", "changed_then_original"])
def test_a_batch_that_changes_its_own_comparison_commits_and_returns(case):
    """Review 2026-11-23: the unchanged check compares with the stored fact, the commit with the batch so far.
    A key deleted and re-upserted unchanged, or a key given twice as [changed, original], must commit (as before
    the skip), not loop."""
    import threading
    from hermes_cli.fact_contract import Fact, FactKey, Scope
    from hermes_cli.fact_store import FactStore

    scope = Scope("install", "default", "board")
    key = FactKey(scope, "TaskFact", "k")
    store = FactStore()
    store.publish((Fact(key, {"v": 1}),))
    result = []
    if case == "delete_then_same":
        work = lambda: result.append(store.publish((Fact(key, {"v": 1}),), deleted_keys=(key,)))
    else:
        work = lambda: result.append(store.publish((Fact(key, {"v": 2}), Fact(key, {"v": 1}))))
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(5)
    assert result, "publish did not return"
    assert result[0] == 2 and store.snapshot(scope).get(key).value["v"] == 1
