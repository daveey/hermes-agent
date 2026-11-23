"""Dirty dependency closure is recomputed once, atomically, without unrelated work."""

import pytest

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_store import FactStore


def test_dirty_closure_uses_staged_inputs_and_publishes_one_generation():
    from hermes_cli.fact_reducers import ReducerEngine, ReducerSpec

    scope = Scope("i", "p")
    source = FactKey(scope, "Input", "a")
    unrelated = FactKey(scope, "Input", "b")
    output = FactKey(scope, "Double", "a")
    other = FactKey(scope, "Double", "b")
    total = FactKey(scope, "Total", "a")
    calls = []
    store = FactStore()
    engine = ReducerEngine(store, scope)

    def double(key, snapshot, now):
        calls.append(key)
        value = snapshot.get(FactKey(scope, "Input", key.id))
        return Fact(key, value.value * 2, health="fresh") if value else None

    def combine(key, snapshot, now):
        calls.append(key)
        return Fact(key, snapshot.get(output).value + 1, health="fresh")

    engine.register(ReducerSpec("double", 1, "Double", ("Input",),
                               lambda key, snap: {FactKey(scope, "Input", key.id)}, double))
    engine.register(ReducerSpec("total", 1, "Total", ("Double",),
                               lambda key, snap: {output}, combine))
    for key in (output, other, total):
        engine.bind(key)
    engine.publish((Fact(source, 1), Fact(unrelated, 10)), now=0)
    before = store.snapshot(scope)
    calls.clear()
    engine.publish((Fact(source, 2),), now=1)
    after = store.snapshot(scope)
    assert calls == [output, total]
    assert after.rev == before.rev + 1
    assert [fact.value for fact in after.get_many((source, output, total, other))] == [2, 4, 5, 20]
    assert [fact.value for fact in before.get_many((source, output, total))] == [1, 2, 3]
    calls.clear()
    engine.publish((Fact(source, 2),), now=2)
    assert calls == []
    assert store.snapshot(scope).rev == after.rev


def test_membership_reads_track_edges_and_failed_reduction_keeps_previous_generation():
    from hermes_cli.fact_reducers import ReducerEngine, ReducerSpec

    scope = Scope("i", "p")
    membership = FactKey(scope, "Membership", "chat")
    a, b = (FactKey(scope, "Input", name) for name in ("a", "b"))
    output = FactKey(scope, "Result", "chat")
    calls = []
    store = FactStore()
    engine = ReducerEngine(store, scope)

    def dependencies(key, snapshot):
        return {FactKey(scope, "Input", snapshot.get(membership).value)}

    def reduce(key, snapshot, now):
        calls.append(key)
        member = FactKey(scope, "Input", snapshot.get(membership).value)
        value = snapshot.get(member)
        if value and value.value == "fail":
            raise ValueError("source invalid")
        return Fact(key, value.value) if value else None

    engine.register(ReducerSpec("member", 1, "Result", ("Membership", "Input"), dependencies, reduce))
    engine.bind(output)
    engine.publish((Fact(membership, "a"), Fact(a, 1), Fact(b, 2)), now=0)
    engine.publish((Fact(membership, "b"),), now=1)
    assert store.snapshot(scope).get(output).value == 2
    calls.clear()
    engine.publish((Fact(a, 3),), now=2)
    assert calls == []
    before = store.snapshot(scope)
    with pytest.raises(ValueError, match="source invalid"):
        engine.publish((Fact(b, "fail"),), now=3)
    assert store.snapshot(scope).rev == before.rev
    assert store.snapshot(scope).get(b).value == 2
    engine.publish(deleted_keys=(b,), now=4)
    assert store.snapshot(scope).get(output) is None
    engine.publish((Fact(b, 4),), now=5)
    assert store.snapshot(scope).get(output).value == 4


def test_reducer_contract_rejects_hidden_kinds_foreign_inputs_and_rebinding():
    from graphlib import CycleError
    from hermes_cli.fact_reducers import ReducerEngine, ReducerSpec

    scope = Scope("i", "p")
    foreign = Scope("i", "other")
    key = FactKey(scope, "Result", "r")
    hidden = FactKey(scope, "Hidden", "x")
    store = FactStore()
    engine = ReducerEngine(store, scope)
    spec = ReducerSpec("r", 1, "Result", ("Input",), lambda key, snap: set(),
                       lambda key, snap, now: Fact(key, snap.get(hidden).value))
    engine.register(spec)
    with pytest.raises(ValueError, match="already registered"):
        engine.register(spec)
    with pytest.raises(CycleError):
        engine.register(ReducerSpec("cycle", 1, "Input", ("Result",),
                                   lambda key, snap: set(), lambda key, snap, now: None))
    with pytest.raises(ValueError, match="scope"):
        engine.bind(FactKey(foreign, "Result", "r"))
    with pytest.raises(ValueError, match="unregistered"):
        engine.bind(hidden)
    with pytest.raises(ValueError, match="scope"):
        engine.publish((Fact(FactKey(foreign, "Input", "r"), 1),), now=0)
    engine.bind(key)
    with pytest.raises(ValueError, match="undeclared input kind"):
        engine.publish((Fact(hidden, 1),), now=0)
    assert store.snapshot(scope).rev == 0


@pytest.mark.parametrize("failure", ["wrong_output", "foreign_edge", "undeclared_edge", "derived_input"])
def test_malformed_batch_cannot_publish_or_install_edges(failure):
    from hermes_cli.fact_reducers import ReducerEngine, ReducerSpec

    scope = Scope("i", "p")
    source = FactKey(scope, "Input", "a")
    output = FactKey(scope, "Result", "a")
    wrong = FactKey(scope, "Result", "b")
    edge = {"foreign_edge": FactKey(Scope("i", "other"), "Input", "a"),
            "undeclared_edge": FactKey(scope, "Hidden", "a")}.get(failure, source)
    store = FactStore()
    engine = ReducerEngine(store, scope)
    engine.register(ReducerSpec("r", 1, "Result", ("Input",), lambda key, snap: {edge},
                               lambda key, snap, now: Fact(wrong if failure == "wrong_output" else key, 1)))
    engine.bind(output)
    with pytest.raises(ValueError, match={"wrong_output": "output key", "foreign_edge": "dependency scope",
                                         "undeclared_edge": "dependency kind", "derived_input": "derived key"}[failure]):
        engine.publish((Fact(output if failure == "derived_input" else source, 1),), now=0)
    assert store.snapshot(scope).rev == 0
