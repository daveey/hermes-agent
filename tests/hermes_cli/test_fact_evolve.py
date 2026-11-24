"""Fact copies share their frozen value; sizing uses the C encoder. [daveey]

On the hub (2026-11-23) the fact-change-feed thread held the GIL ~55% of the time: dataclasses.replace() re-froze
every copied Fact's whole value (~27%) and encoded_size() ran json's pure-Python encoder (~25%).
"""
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest

from hermes_cli import fact_contract, fact_wire
from hermes_cli.fact_contract import Fact, FactKey, Scope, SourceId, evolve
from hermes_cli.fact_reducers import ReducerEngine, ReducerSpec
from hermes_cli.fact_source_commit import SourcePublisher
from hermes_cli.fact_store import FactStore
from hermes_cli.fact_wire import encode, encoded_size

SCOPE = Scope("install", "profile", "board")


def _big(n=200):
    return {"rows": [{"id": str(i), "tags": ["a", "b"], "meta": {"n": i}} for i in range(n)]}


@pytest.fixture
def freezes(monkeypatch):
    """Top-level freeze() walks (freeze recurses through the module name, so nested calls are not counted)."""
    calls = []
    depth = [0]
    real = fact_contract.freeze

    def counting(value):
        if depth[0] == 0:
            calls.append(type(value).__name__)
        depth[0] += 1
        try:
            return real(value)
        finally:
            depth[0] -= 1

    monkeypatch.setattr(fact_contract, "freeze", counting)
    return calls


def test_evolve_matches_replace_and_shares_the_frozen_value(freezes):
    fact = Fact(FactKey(SCOPE, "View", "v"), _big(), rev=3, source_vector=[{"c": [1]}], observed_at=1.5,
                valid_until=9.0, health="fresh")
    for changes in ({"rev": 7}, {"health": "stale", "error_code": "x"}, {"valid_until": None},
                    {"rev": 0, "health": "unknown", "observed_at": 2.0}):
        expected = replace(fact, **changes)
        freezes.clear()
        copy = evolve(fact, **changes)
        assert freezes == []
        assert copy == expected
        assert copy.value is fact.value and copy.source_vector is fact.source_vector
        assert hash(copy.key) == hash(fact.key)


def test_evolve_freezes_a_new_value_or_source_vector():
    fact = Fact(FactKey(SCOPE, "View", "v"), {"a": 1})
    raw = {"a": [1, {"b": 2}]}
    copy = evolve(fact, value=raw, source_vector=[{"s": [1]}])
    raw["a"][1]["b"] = 3
    assert copy.value["a"] == (1, {"b": 2})
    assert copy.source_vector == ({"s": (1,)},)
    with pytest.raises(TypeError):
        copy.value["a"] = 2
    assert copy == Fact(FactKey(SCOPE, "View", "v"), {"a": [1, {"b": 2}]}, source_vector=[{"s": [1]}])


def test_evolve_refuses_unknown_fields_and_non_facts():
    fact = Fact(FactKey(SCOPE, "View", "v"), 1)
    with pytest.raises(TypeError):
        evolve(fact, revision=2)
    with pytest.raises(TypeError):
        evolve(SimpleNamespace(rev=1), rev=2)


def test_store_publish_freezes_nothing_for_unchanged_or_changed_facts(freezes):
    store = FactStore()
    facts = [Fact(FactKey(SCOPE, "View", str(i)), _big(20)) for i in range(5)]
    store.publish(facts)
    changed = Fact(facts[0].key, {"other": [1, 2]})
    freezes.clear()
    store.publish(facts[1:])  # unchanged republish
    store.publish([changed])
    assert freezes == []
    assert store.snapshot(SCOPE).get(facts[0].key).value == {"other": (1, 2)}


def test_reducer_publish_freezes_only_what_reducers_build(freezes):
    store = FactStore()
    engine = ReducerEngine(store, SCOPE)
    source = FactKey(SCOPE, "Input", "a")
    output = FactKey(SCOPE, "View", "out")
    engine.register(ReducerSpec("view", 1, "View", ("Input",), lambda key, snap: {source},
                                lambda key, snap, now: Fact(key, {"n": snap.get(source).value}, health="fresh")))
    engine.bind(output)
    engine.publish([Fact(source, 1)], now=0)
    upsert = Fact(source, 2)
    freezes.clear()
    engine.publish([upsert], now=1)
    # one value + one source_vector freeze for the reducer's new output; none for the copies on the way in
    assert len(freezes) == 2
    assert store.snapshot(SCOPE).get(output).value == {"n": 2}


@dataclass(frozen=True)
class _Cursor:
    n: int


def test_source_commit_freezes_each_row_once(freezes):
    store = FactStore()
    engine = ReducerEngine(store, SCOPE)
    publisher = SourcePublisher(engine, SourceId(SCOPE, "sessions", "local"), "RowInput")
    rows = [{"id": str(i), "title": f"t{i}", "tags": ["a"]} for i in range(10)]

    def capture(before, after, records):
        return SimpleNamespace(before=before, after=after, ids=[r["id"] for r in records], records=records,
                               full=True, observed_at=float(after.n))

    publisher.commit(capture(None, _Cursor(1), rows))
    unchanged = [dict(row) for row in rows]
    unchanged[0]["title"] = "changed"
    freezes.clear()
    publisher.commit(capture(_Cursor(1), _Cursor(2), unchanged))
    # per row: Fact.__post_init__ freezes value + source_vector once (2), never a second compare-only freeze;
    # the state fact adds 2 more. The membership fact is unchanged and not rebuilt.
    assert len(freezes) == 2 * len(rows) + 2
    snap = store.snapshot(SCOPE)
    assert snap.get(publisher.key("0")).value["title"] == "changed"
    assert snap.get(publisher.key("1")).observed_at == 1.0  # unchanged row keeps its provenance


@pytest.mark.parametrize("value", [
    {"a": [1, 2.5, None, True], "s": "héllo ☃  ", "b": b"\x00\xff" * 40},
    Fact(FactKey(SCOPE, "View", "v"), _big(50), source_vector=[{"c": 1}], health="fresh"),
    frozenset({FactKey(SCOPE, "K", "x"), "y"}),
    (),
])
def test_encoded_size_is_the_encoded_length(value):
    assert encoded_size(value) == len(encode(value))


@pytest.mark.parametrize("length", [0, 1, 2, 3, 4, 65535, 65536, 65537, 3 * 65536, 200_001])
def test_encoded_size_counts_bytes_exactly(length):
    data = bytes(range(256)) * (length // 256 + 1)
    data = data[:length]
    for value in (data, {"encoded": data, "n": 1}, [data, data[:7]],
                  Fact(FactKey(SCOPE, "View", "v"), {"encoded": data}, health="fresh")):
        assert encoded_size(value) == len(encode(value))


def test_encoded_size_uses_neither_the_python_encoder_nor_base64(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("slow or copying sizing path used")

    value = Fact(FactKey(SCOPE, "View", "v"), dict(_big(5), encoded=b"x" * 300_000))
    expected = len(encode(value))
    monkeypatch.setattr(json.encoder, "_make_iterencode", refuse)
    monkeypatch.setattr(fact_wire.base64, "b64encode", refuse)
    assert encoded_size(value) == expected
