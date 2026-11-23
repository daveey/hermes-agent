"""Authorized mixed-profile annotation lookup, without request-side resolution."""
from copy import deepcopy
import time

import pytest

from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_annotation_startup import AnnotationRegistry, binding_for
from hermes_cli.fact_annotation_views import projections, selector_for
from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_materializers import view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore, view_key
from hermes_cli.goal_session_activity import annotate_goal_activity
from tests.hermes_cli.annotation_fact_processes import request_guard


def test_real_mixed_profile_automatic_lookup_order_atomic_failure_and_custody():
    now = time.time()
    store = FactStore()
    scopes = {name: Scope('mixed-startup', name) for name in ('default', 'named')}
    engines = {name: ReducerEngine(store, scope) for name, scope in scopes.items()}
    life = IncrementalLifecycle({(scope, 'annotations') for scope in scopes.values()})
    local = InProcessFacts(store, set(scopes.values()), demand=lambda *args: None)
    handles, bindings = {}, {}
    rows = [{'id': 's', 'profile': 'named'}, {'id': 's', 'profile': 'default'}, {'id': 's', 'profile': 'named'}]
    for name, scope in scopes.items():
        engines[name].register(view_reducer(projections()))
        engines[name].bind(view_key(scope, 'annotations.goal', selector_for(scope, {'id': 's', 'profile': name})))
        engines[name].publish([Fact(FactKey(scope, 'AnnotationInput', 'goal'),
            {'active': ['s'] if name == 'default' else []}, health='fresh', observed_at=now,
            valid_until=now + 90)], now=now)
        handles[name] = local.acquire(scope, lifecycle=life)
        bindings[name] = annotations.AnnotationFacts(handles[name], scope, stop_legacy=lambda: None,
            start_legacy=lambda: None, view_facts=True)
        life.report_ready('annotations', scope)
        life.transition('annotations', scope, 'facts', 1)
    registry = AnnotationRegistry(bindings, 'default').install()
    try:
        with request_guard():
            assert binding_for('named') is bindings['named']
            assert binding_for() is bindings['default']
            assert binding_for('foreign') is None
            result = annotate_goal_activity(deepcopy(rows))
        assert [row.get('goal_active', False) for row in result] == [False, True, False]
        assert [row['profile'] for row in result] == ['named', 'default', 'named']
        with pytest.raises(RuntimeError):
            AnnotationRegistry(bindings, 'default').install()
        # A failed named profile cannot leave a successful default patch behind.
        engines['named'].publish([Fact(FactKey(scopes['named'], 'AnnotationInput', 'goal'), {}, health='unknown')], now=now)
        pending = [dict(rows[1]), dict(rows[0])]
        original = deepcopy(pending)
        with request_guard(), pytest.raises(annotations.AnnotationUnavailable):
            annotate_goal_activity(pending)
        assert pending == original
        with request_guard(), pytest.raises(annotations.AnnotationUnavailable):
            annotate_goal_activity([{'id': 's', 'profile': 'foreign'}])
        registry.close()
        successor = AnnotationRegistry(bindings, 'default').install()
        try:
            registry.close()
            assert binding_for() is bindings['default']
        finally:
            successor.close()
    finally:
        registry.close()
        for name, scope in scopes.items():
            life.transition('annotations', scope, 'legacy', 2)
            bindings[name].close()
            handles[name].close()
        local.close()
    assert binding_for() is None


def test_registry_rejects_mismatched_profile_handle():
    from types import SimpleNamespace
    binding = SimpleNamespace(scope=Scope('scope', 'named'))
    with pytest.raises(ValueError):
        AnnotationRegistry({'default': binding}, 'default')
    with pytest.raises(ValueError):
        AnnotationRegistry({'named': binding}, 'absent')
