"""Unchanged derived payloads must not be re-encoded on source clock advances."""
from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_reducers import ReducerEngine, ReducerSpec
from hermes_cli.fact_store import FactStore
from hermes_cli import fact_store


def test_quiet_derived_outputs_skip_wire_work_but_keep_dependency_rebinding(monkeypatch):
    scope = Scope('i', 'p')
    clock = FactKey(scope, 'Input', 'clock')
    a, b = (FactKey(scope, 'Input', name) for name in ('a', 'b'))
    output = FactKey(scope, 'View', 'large')
    downstream = FactKey(scope, 'Downstream', 'value')
    engine = ReducerEngine(FactStore(), scope)
    seen = []

    def reduce(key, snapshot, now):
        member = a if snapshot.get(clock).value == 0 else b
        value = snapshot.get(member)
        return Fact(key, {'payload': 'x' * 100_000, 'value': value.value}, health='fresh') if value else None

    def next_reduce(key, snapshot, now):
        value = snapshot.get(output)
        seen.append(None if value is None else value.value['value'])
        return Fact(key, seen[-1]) if value else None

    engine.register(ReducerSpec('view', 1, 'View', ('Input',), lambda key, snap: set(), reduce))
    engine.register(ReducerSpec('downstream', 1, 'Downstream', ('View',),
                               lambda key, snap: {output}, next_reduce))
    engine.bind(output)
    engine.bind(downstream)
    engine.publish([Fact(clock, 0), Fact(a, 1), Fact(b, 1)], now=0)
    before = engine.store.snapshot(scope)
    encoded = []
    original = fact_store.encoded_size

    def count(fact):
        encoded.append(fact.key)
        return original(fact)

    monkeypatch.setattr(fact_store, 'encoded_size', count)
    # Switch dependency but retain the identical large result. Downstream must
    # see the prior fact even when it is omitted from the publication overlay.
    engine.publish([Fact(clock, 1)], now=1)
    assert seen == [1, 1]
    assert encoded == [clock]
    assert engine.store.snapshot(scope).get(output) is before.get(output)
    encoded.clear()
    engine.publish([Fact(a, 2)], now=2)
    assert seen == [1, 1]  # unchanged result still installed its new edges
    engine.publish([Fact(b, 3)], now=3)
    assert seen[-1] == 3
    assert output in encoded and downstream in encoded
    assert engine.store.snapshot(scope).get(output).value['value'] == 3
    engine.publish(deleted_keys=[b], now=4)
    assert seen[-1] is None
    assert engine.store.snapshot(scope).get(output) is None
    assert engine.store.snapshot(scope).get(downstream) is None
    assert before.get(output).value['value'] == 1
