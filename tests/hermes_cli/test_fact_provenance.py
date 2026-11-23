"""Provenance remains immutable and names the generation actually published."""

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_store import FactStore


def test_source_vector_detaches_nested_mutable_entries():
    scope = Scope("i", "p")
    key = FactKey(scope, "Input", "a")
    entry = {"source": "origin", "cursor": [1]}
    store = FactStore()
    store.publish((Fact(key, 1, source_vector=(entry,)),))
    snapshot = store.snapshot(scope)
    entry["cursor"][0] = 999
    assert snapshot.get(key).source_vector[0]["cursor"] == (1,)


def test_staged_revisions_match_committed_source_and_derived_facts():
    from hermes_cli.fact_reducers import ReducerEngine, ReducerSpec

    scope = Scope("i", "p")
    source = FactKey(scope, "Input", "a")
    middle = FactKey(scope, "Middle", "a")
    output = FactKey(scope, "Output", "a")
    store = FactStore()
    engine = ReducerEngine(store, scope)
    for kind, dependency in (("Middle", source), ("Output", middle)):
        engine.register(ReducerSpec(kind, 1, kind, (dependency.kind,),
                                   lambda key, snap, dep=dependency: {dep},
                                   lambda key, snap, now, dep=dependency: Fact(key, snap.get(dep).rev)))
    engine.bind(middle)
    engine.bind(output)
    for value in (1, 2):
        engine.publish((Fact(source, value, rev=999),), now=value)
        snapshot = store.snapshot(scope)
        assert snapshot.get(middle).value == snapshot.get(source).rev == value
        assert snapshot.get(output).value == snapshot.get(middle).rev == value
