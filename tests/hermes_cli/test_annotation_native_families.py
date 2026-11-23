"""All captured families through real selectors, reducers and decorated callers."""
from copy import deepcopy
import importlib
import time

import pytest

from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_annotation_views import projections, selector_for
from hermes_cli.fact_contract import Fact, FactKey, Scope, SourceId
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_materializers import view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore, view_key
from tests.hermes_cli.annotation_fact_processes import request_guard


FAMILIES = (
    ('kanban', 'kanban_session_activity', 'annotate_kanban_activity'),
    ('background', 'process_session_activity', 'annotate_background_activity'),
    ('turn', 'turn_session_activity', 'annotate_turn_activity'),
    ('subagent', 'subagent_session_activity', 'annotate_subagent_activity'),
    ('review', 'review_session_activity', 'annotate_review_wait'),
)


def captured(family, now):
    if family == 'kanban':
        from hermes_cli.kanban_execution_observations import card
        execution = {'root': {'default/t1': card({'id': 't1', 'board': 'default', 'status': 'running',
            'current_run_id': 7}, gateway='gateway', profile='default', now=now)}}
        parts = ({'root': 2}, {'root': 1}, {'root': 1}, {}, {'root': {'t2', 't1'}},
                 {'root': 1}, {'root': 1}, {},
                 {'root': {('default/t1', 'active', True), ('default/t2', 'blocked', False),
                           ('peer/default/t3', 'external_unknown', True)}}, {}, {'root': 1}, execution)
        return {'parts': parts, 'observed_at': now - 3, 'external': {
            'root': {'counts': [1, 1, 0], 'cards': [{'task_id': 'external', 'title': '',
                'external': {'observed_at': None, 'url': None, 'next_due_at': None}}]}}}
    if family == 'background':
        return {'owned': {'root': {'p2', 'p1'}}, 'details': {
            'p1': {'id': 'p1', 'command': 'first', 'started_at': now - 100, 'pid': 123},
            'p2': {'id': 'p2', 'command': 'second'}}, 'evidence': {},
            'gateway': 'gateway', 'local_profile': 'default'}
    if family == 'turn':
        return {'live': {'root'}, 'leases': {}, 'gateway': 'gateway'}
    if family == 'subagent':
        return {'by_profile': {'default': ([('child', 'root', now - 1, now - 20)], None)},
                'gateway': 'gateway'}
    return {'messages': {'root': (100, [('owner', 'repo', 17)]), 's': (99, [])},
            'reviews': [(('owner', 'repo', 17), {'open': True, 'review_required': True,
                'viewer': 'reviewer', 'author': 'author', 'requested': ['user:reviewer'],
                'requested_at': now - 100})]}


def canonical(family, module, rows, value, now):
    if family == 'kanban':
        from hermes_cli.kanban_activity_projection import project_kanban_activity
        from hermes_cli.kanban_external_activity import stamp_replica_counts
        from hermes_cli.kanban_activity_identity import row_ids
        project_kanban_activity(rows, **value, now=now)
        stamp_replica_counts(rows, value['parts'][8], row_ids)
    else:
        project = {
            'background': lambda: module.project_background_activity(rows, **value, now=now),
            'turn': lambda: module.project_turn_activity(rows, **value, now=now),
            'subagent': lambda: module.project_subagent_activity(rows, **value, now=now),
            'review': lambda: module.project_review_row(rows[0], reviews=value['reviews'], now=now),
        }[family]
        project()
    return rows


@pytest.mark.parametrize('family,module_name,entry_name', FAMILIES)
def test_every_native_family_reuses_canonical_serializers_and_real_entry(
        family, module_name, entry_name, monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, 'time', lambda: now)
    module = importlib.import_module('hermes_cli.' + module_name)
    scope = Scope('native-families', 'default')
    store = FactStore()
    engine = ReducerEngine(store, scope)
    engine.register(view_reducer(projections()))
    row = {'id': 's', 'profile': 'default', '_lineage_ids': ['root'], 'title': 'keep'}
    selector = selector_for(scope, row)
    key = view_key(scope, 'annotations.' + family, selector)
    engine.bind(key)
    value = captured(family, now)
    source = SourceId(scope, 'annotations.' + family, 'local')
    fact = Fact(FactKey(scope, 'AnnotationInput', family), value, health='fresh',
                source_vector=((source, 'capture-1'),), observed_at=now, valid_until=now + 90)
    local = InProcessFacts(store, {scope}, demand=lambda *args: pytest.fail('unexpected demand'))
    life = IncrementalLifecycle({(scope, 'annotations')})
    handle = local.acquire(scope, lifecycle=life)
    consumer = annotations.AnnotationFacts(handle, scope, stop_legacy=lambda: None,
                                          start_legacy=lambda: None, view_facts=True)
    try:
        with request_guard():
            projection = next(p for p in projections() if p.name == 'annotations.' + family)
            projection.render(selector, {fact.key: fact}, now)
            engine.publish([fact], now=now)
        projected = store.snapshot(scope).get(key)
        assert projected.health == 'fresh', projected.error_code
        assert projected.source_vector == ((source, 'capture-1'),)
        expected = canonical(family, module, [deepcopy(row)], value, now)
        assert set(expected[0]) - set(row), 'nonempty family required'
        life.report_ready('annotations', scope)
        life.transition('annotations', scope, 'facts', 1)
        entry = getattr(module, entry_name)
        with annotations.use(consumer), request_guard():
            actual = entry([deepcopy(row)])
        assert actual == expected
        # Explicit JSON nulls and ordered arrays survive immutable capture and thaw.
        assert actual[0]['title'] == 'keep'
        assert fact.value == store.snapshot(scope).get(fact.key).value
        life.transition('annotations', scope, 'legacy', 2)
    finally:
        consumer.close()
        handle.close()
        local.close()


@pytest.mark.parametrize('missing', ['lineage', 'provider'])
def test_review_unhydrated_or_provider_unknown_is_not_fresh_empty(missing):
    scope = Scope('review-readiness', 'default')
    row = {'id': 's', '_lineage_ids': ['root']}
    value = captured('review', time.time())
    if missing == 'lineage':
        del value['messages']['root']
    else:
        value['reviews'][0] = (value['reviews'][0][0], None)
    key = FactKey(scope, 'AnnotationInput', 'review')
    fact = Fact(key, value, health='fresh')
    projection = next(p for p in projections() if p.name == 'annotations.review')
    with pytest.raises(ValueError), request_guard():
        projection.render(selector_for(scope, row), {key: fact}, time.time())


def test_quiet_review_clock_boundary_reduces_even_with_identical_input(monkeypatch):
    from hermes_cli.fact_annotation_membership import MembershipCapture
    from hermes_cli.fact_annotation_sources import NativeAnnotationSource
    clock = [1000.0]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    scope = Scope('quiet-review-clock', 'default')
    store = FactStore()
    engine = ReducerEngine(store, scope)
    engine.register(view_reducer(projections()))
    row = {'id': 's', '_lineage_ids': ['root']}
    key = view_key(scope, 'annotations.review', selector_for(scope, row))
    engine.bind(key)
    value = captured('review', clock[0])
    info = value['reviews'][0][1]
    info.update(viewer='author', requested=['user:someone'], requested_at=1001 - 86400)
    source = NativeAnnotationSource(engine, SourceId(scope, 'annotations.review', 'local'), [], None)
    source.commit(MembershipCapture(value, 'unchanged', 1000, 1001))
    assert store.snapshot(scope).get(key).value['payload']['review_wait'] == 'parked'
    # A no-content hint cannot push the outstanding clock boundary back.
    clock[0] = 1000.5
    source.commit(MembershipCapture(value, 'unchanged', 1000.5, 1001))
    assert source._next_wall == 1001
    clock[0] = 1001
    source.commit(MembershipCapture(value, 'unchanged', 1001, 1031))
    assert store.snapshot(scope).get(key).value['payload']['review_wait'] == 'stuck'
