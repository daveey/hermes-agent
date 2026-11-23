"""Selector-aware native annotation bridge; legacy mode remains explicit."""
from copy import deepcopy

import pytest

from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_store import FactStore, view_key


@pytest.fixture
def bridge():
    scope = Scope('annotation-views', 'default')
    store = FactStore()
    demands = []
    life = IncrementalLifecycle({(scope, 'annotations')})
    owner = InProcessFacts(store, {scope}, demand=lambda *args: demands.append(args))
    handle = owner.acquire(scope, lifecycle=life)
    consumer = annotations.AnnotationFacts(handle, scope, stop_legacy=lambda: None,
                                          start_legacy=lambda: None, view_facts=True)
    life.report_ready('annotations', scope)
    life.transition('annotations', scope, 'facts', 1)
    try:
        yield scope, store, consumer, demands
    finally:
        life.transition('annotations', scope, 'legacy', 2)
        consumer.close()
        handle.close()
        owner.close()


def test_lookup_and_demand_share_lineage_display_identity(bridge):
    from hermes_cli.fact_annotation_views import selector_for
    scope, store, consumer, demands = bridge
    row = {'id': 's', '_lineage_ids': ['old', 'older'], '_lineage_root_id': 'root'}
    selector = selector_for(scope, row, display=True)
    key = view_key(scope, 'annotations.kanban', selector)
    store.publish([Fact(key, {'payload': {'kanban_active': 2, 'kanban_parked': 0}}, health='fresh')])
    actual = deepcopy(row)
    consumer.annotate('kanban', [actual], None, display=True)
    assert actual['kanban_active'] == 2
    untouched = deepcopy(row)
    with pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate('kanban', [untouched], None, display=False)
    assert untouched == row
    assert demands[-1] == (scope, 'annotations.kanban', selector_for(scope, row, display=False))
    other = {**row, '_lineage_ids': ['different']}
    with pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate('kanban', [other], None, display=True)
    assert demands[-1][2] != selector


def test_variant_batch_never_partly_mutates(bridge):
    from hermes_cli.fact_annotation_views import selector_for
    scope, store, consumer, _ = bridge
    rows = [{'id': 'a'}, {'id': 'b', 'goal_active': True}]
    before = deepcopy(rows)
    key = view_key(scope, 'annotations.goal', selector_for(scope, rows[0]))
    store.publish([Fact(key, {'payload': {'goal_active': True}}, health='fresh')])
    with pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate('goal', rows, None)
    assert rows == before


def test_canonical_projection_uses_captured_goal_input():
    from hermes_cli.fact_annotation_views import projections, selector_for
    from hermes_cli.fact_materializers import view_reducer
    from hermes_cli.fact_reducers import ReducerEngine
    scope = Scope('projection', 'default')
    store = FactStore()
    engine = ReducerEngine(store, scope)
    engine.register(view_reducer(projections()))
    key = view_key(scope, 'annotations.goal', selector_for(scope, {'id': 's', '_lineage_ids': ['root']}))
    engine.bind(key)
    source = FactKey(scope, 'AnnotationInput', 'goal')
    engine.publish([Fact(source, {'active': ['root']}, health='fresh', observed_at=10, valid_until=50)], now=20)
    fact = store.snapshot(scope).get(key)
    assert fact.value['payload'] == {'goal_active': True}
    assert fact.valid_until == 50
    engine.publish([Fact(source, {}, health='stale', observed_at=10, valid_until=50)], now=60)
    assert store.snapshot(scope).get(key).health == 'stale'


def test_rejected_demand_and_invalid_row_are_retryable(bridge, monkeypatch):
    _, _, consumer, _ = bridge
    def refused(*args):
        raise ValueError('unregistered or capacity exceeded')
    monkeypatch.setattr(consumer.handle, 'demand', refused)
    with pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate('review', [{'id': 's'}], None)
    with pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate('goal', [{'id': 's', '_lineage_ids': 7}], None)
