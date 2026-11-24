"""Hub-sized fact inventory costs without relaxing byte/time or cold-read gates."""
from collections.abc import Mapping
import json
import threading
import time

import pytest

from hermes_cli import fact_contract as contract, fact_wire as wire
from hermes_cli.fact_board_inputs import BoardInputSource, CapturedInput
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore


class ScalarMapping(str, Mapping):
    """An unusual supported value: mapping dispatch must still precede scalar fallback."""
    def __iter__(self):
        return iter(('key',))

    def __len__(self):
        return 1

    def __getitem__(self, key):
        if key != 'key':
            raise KeyError(key)
        return 7


def test_scalar_mapping_subclass_keeps_historical_precedence():
    value = ScalarMapping('text')
    assert contract.freeze(value) == {'key': 7}
    assert wire.pack(value) == ['map', [['key', 7]]]
    assert wire.decode(wire.encode(value)) == {'key': 7}


@pytest.mark.parametrize('scalar', [None, 'text', True, False, 2, -3, 4.5])
def test_scalar_leaf_pack_does_not_dispatch_dataclass(monkeypatch, scalar):
    calls = []
    original = wire.is_dataclass

    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(wire, 'is_dataclass', counted)
    assert wire.pack(scalar) == scalar
    assert calls == []


@pytest.mark.parametrize('scalar', [None, 'text', b'raw', True, 2, 4.5])
def test_scalar_leaf_freeze_does_not_dispatch_mapping(monkeypatch, scalar):
    calls = []
    real_mapping = contract.Mapping

    class CountedMapping(type):
        def __instancecheck__(cls, value):
            calls.append(value)
            return isinstance(value, real_mapping)

    monkeypatch.setattr(contract, 'Mapping', CountedMapping('Probe', (), {}))
    assert contract.freeze(scalar) is scalar
    assert calls == []


def inventory(n):
    return [{'id': str(i), 'title': 'task ' + str(i), 'status': 'ready',
             'body': 'x' * 1000, 'counts': [i, 0, None, False], 'origin': {'profile': 'default'}}
            for i in range(n)]


def test_hub_sized_shared_inventory_stays_inside_original_budget():
    # Measured hub: 4,590 link cards+receipts (~6.9 MB), 264 displayed board cards,
    # and 263 annotation aggregate entries. Fixtures are synthetic, never production payloads.
    scope = contract.Scope('installation', 'default', 'default')
    store = FactStore()
    links, board = inventory(4590), inventory(264)
    payload = {'columns': [{'name': 'ready', 'tasks': board}]}
    encoded = json.dumps(payload, separators=(',', ':')).encode()
    for revision in range(3):
        start = time.thread_time()
        facts = [contract.Fact(contract.FactKey(scope, kind, ''), value,
                              observed_at=revision, valid_until=revision + 90, health='fresh')
                 for kind, value in [('RemoteLinksInput', {'cards': links, 'origin_rows': []}),
                                     ('BoardInput', board),
                                     ('ViewFact', {'payload': payload, 'encoded': encoded}),
                                     ('AnnotationInput', {str(i): i for i in range(263)})]]
        store.publish(facts)
        assert time.thread_time() - start < CaptureBudget(max_seconds=10).max_seconds
        assert store._fact_bytes < 48 * 1024 * 1024
        for fact in facts:
            assert wire.encoded_size(fact) == len(wire.encode(fact))
            assert wire.decode(wire.encode(fact)) == fact
    assert json.loads(store.snapshot(scope).get(facts[2].key).value['encoded']) == payload


def test_write_storm_coalesces_one_trailing_capture(monkeypatch):
    scope = contract.Scope('installation', 'default', 'default')
    engine = ReducerEngine(FactStore(), scope)
    source_id = contract.SourceId(scope, 'board.input', 'native')
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def capture(*args):
        calls.append(len(calls))
        entered.set()
        assert release.wait(5)
        return CapturedInput({'rows': inventory(264)}, ((source_id, len(calls)),), time.time())

    source = BoardInputSource(engine, source_id, capture)
    source.watch(completed.set)
    budget = CaptureBudget(max_seconds=10)
    try:
        assert source.want('board', {})
        assert source.capture(None, budget).key is None
        assert entered.wait(5)
        for _ in range(10000):
            source._invalidate()
        assert len(source._pending) == 1
        assert len(calls) == 1
        completed.clear()
        release.set()
        assert completed.wait(5)
        source._worker.join(5)
        result = source.capture(None, budget)
        source.commit(result)
        # Writes retained during the first capture must wait for its original min-gap, not
        # launch 10,000 jobs. Advance only this selector's admission clock, not global time.
        assert source.capture(None, budget).key is None
        assert len(calls) == 1
        source._last_start[source.input_key('board', {})] -= 5
        completed.clear()
        source.capture(None, budget)
        assert completed.wait(5)
        source._worker.join(5)
        source.commit(source.capture(None, budget))
        assert len(calls) == 2
        assert not source._pending
        fact = engine.store.snapshot(scope).get(source.input_key('board', {}))
        assert fact.health == 'fresh'
        assert len(fact.value['rows']) == 264
    finally:
        release.set()
        source.close()
