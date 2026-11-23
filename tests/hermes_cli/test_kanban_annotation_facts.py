"""Shared FakeStore integration: annotation consumers never hydrate on reads."""
from contextlib import contextmanager
from types import SimpleNamespace
import builtins
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hermes_cli.fact_contract import Fact, FactKey, Scope
from hermes_cli.fact_fake import FakeStore
from hermes_cli import kanban_annotation_facts as annotations


@pytest.fixture
def setup():
    store = FakeStore()
    scope = Scope('install', 'default')
    demand, lifecycle = [], []
    handle = SimpleNamespace(v1=store, demand=lambda *args: demand.append(args))
    consumer = annotations.AnnotationFacts(handle, scope,
        stop_legacy=lambda: lifecycle.append('stop'), start_legacy=lambda: lifecycle.append('start'))
    for name in ('sessions', 'panel', 'board', 'notifications'):
        store.bind_consumer(name, scope, lambda *args: None)
        store.report_ready(name, scope)
    store.report_ready('annotations', scope)
    store.activate(scope)
    yield store, scope, consumer, demand, lifecycle
    consumer.close()


def test_holder_suspend_blocks_restart_and_resume_is_idempotent(monkeypatch):
    from hermes_cli import kanban_annotation_host as host
    calls = []
    monkeypatch.setattr(host, '_holder_suspended', False)
    monkeypatch.setattr(host, 'stop_holder', lambda **kwargs: calls.append('stop'))
    monkeypatch.setattr(host, '_start_holder', lambda role: calls.append(role))
    host.suspend_holder()
    host.start_holder('request-race')
    assert calls == ['stop']
    host.resume_holder('serve')
    host.resume_holder('serve')
    assert calls == ['stop', 'stop', 'serve']


def publish(store, scope, sid, family, patch, **kwargs):
    store.publish([Fact(FactKey(scope, 'SessionFact', sid),
        {'annotations': {family: patch}}, health='fresh', **kwargs)])


def forbidden(*args, **kwargs):
    pytest.fail('request performed source I/O')


@contextmanager
def no_io(monkeypatch):
    with monkeypatch.context() as guard:
        for obj, name in ((builtins, 'open'), (Path, 'open'), (Path, 'stat'),
                          (sqlite3, 'connect'), (socket, 'socket'), (subprocess, 'Popen')):
            guard.setattr(obj, name, forbidden)
        yield


@pytest.mark.parametrize('family,module,name', [
    ('kanban', 'kanban_session_activity', 'annotate_kanban_activity'),
    ('goal', 'goal_session_activity', 'annotate_goal_activity'),
    ('cron', 'cron_session_activity', 'annotate_cron_activity'),
    ('background', 'process_session_activity', 'annotate_background_activity'),
    ('turn', 'turn_session_activity', 'annotate_turn_activity'),
    ('subagent', 'subagent_session_activity', 'annotate_subagent_activity'),
    ('review', 'review_session_activity', 'annotate_review_wait'),
])
def test_real_annotation_entrypoints_do_not_read_sources(setup, monkeypatch, family, module, name):
    import importlib
    implementation = importlib.import_module('hermes_cli.' + module)
    entry = getattr(implementation, name)
    store, scope, consumer, *_ = setup
    publish(store, scope, 's', family, {})
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli import kanban_annotation_disk as disk
    for obj, attr in ((activity, '_annotation_snapshot'), (activity, '_open_ro'),
                      (activity, 'cached_ro'), (disk, 'load_host'), (disk, 'load')):
        monkeypatch.setattr(obj, attr, forbidden)
    with annotations.use(consumer), no_io(monkeypatch):
        assert entry([{'id': 's'}]) == [{'id': 's'}]


def test_shared_store_read_and_exact_absence_without_io(setup, monkeypatch):
    store, scope, consumer, demand, _ = setup
    payload = {'kanban_active': 1, 'kanban_parked': 0, 'kanban_waiting_on': ['task'],
               'execution_observations': []}
    publish(store, scope, 's', 'kanban', payload)
    row = {'id': 's', 'title': 'unchanged', 'kanban_held': 3}
    with annotations.use(consumer), no_io(monkeypatch):
        assert annotations.annotate('kanban', [row], forbidden) == [
            {'id': 's', 'title': 'unchanged', **payload}]
    assert demand == []
    row['kanban_waiting_on'].append('caller-owned')
    assert store.snapshot(scope).get(FactKey(scope, 'SessionFact', 's')).value[
        'annotations']['kanban']['kanban_waiting_on'] == ('task',)


@pytest.mark.parametrize('family', ('kanban', 'background', 'turn', 'subagent'))
def test_observations_keep_other_families(setup, family):
    store, scope, consumer, *_ = setup
    source = sorted(annotations.SOURCES[family])[0]
    kept = {'source': 'foreign', 'state': 'unknown'}
    fresh = {'source': source, 'state': 'waiting'}
    publish(store, scope, 's', family, {'execution_observations': [fresh]})
    row = {'id': 's', 'execution_observations': [kept, {'source': source, 'state': 'running'}]}
    consumer.annotate(family, [row], forbidden)
    assert row['execution_observations'] == [kept, fresh]


def test_missing_second_row_is_atomic_and_memory_only(setup, monkeypatch):
    store, scope, consumer, demand, _ = setup
    publish(store, scope, 's', 'goal', {'goal_active': True})
    rows = [{'id': 's'}, {'id': 'missing'}]
    with no_io(monkeypatch), pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate('goal', rows, forbidden)
    assert rows == [{'id': 's'}, {'id': 'missing'}]
    assert demand == [('annotations', {'session_id': 'missing'})]


def test_authority_and_profile_isolation(setup):
    store, scope, consumer, *_ = setup
    publish(store, Scope('other', 'default'), 's', 'goal', {'goal_active': True})
    with pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate('goal', [{'id': 's'}], forbidden)
    publish(store, scope, 's', 'goal', {'goal_active': True})
    with pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate('goal', [{'id': 's', 'profile': 'other'}], forbidden)


def test_health_and_expiry_do_not_fallback_then_quiet_recovery(setup):
    store, scope, consumer, *_ = setup
    key = FactKey(scope, 'SessionFact', 's')
    store.publish([Fact(key, {'annotations': {'goal': {'goal_active': True}}}, health='stale')])
    with pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate('goal', [{'id': 's'}], forbidden)
    publish(store, scope, 's', 'goal', {'goal_active': True}, valid_until=1)
    with pytest.raises(annotations.AnnotationUnavailable):
        consumer.annotate('goal', [{'id': 's'}], forbidden)
    publish(store, scope, 's', 'goal', {'goal_active': True})
    assert consumer.annotate('goal', [{'id': 's'}], forbidden) == [{'id': 's', 'goal_active': True}]


def test_real_kanban_serializer_parity_including_lineage_and_waits(setup, monkeypatch):
    from copy import deepcopy
    from hermes_cli import kanban_session_activity as activity
    store, scope, consumer, *_ = setup
    monkeypatch.setattr(activity.time, 'time', lambda: 1000.0)
    parts = ({'s': 2}, {'s': 1}, {'s': 1}, {}, {'s': {'t1', 't2'}},
             {'s': 1}, {'s': 1}, {},
             {'s': {('default/t1', 'active', True), ('default/t2', 'blocked', False)}},
             {'s': {'default/t1', 'extra/t3'}}, {'s': 1}, {})
    monkeypatch.setattr(activity, '_annotation_snapshot', lambda **kwargs: parts)
    monkeypatch.setattr(activity._annotation_copy, 'observed_at', lambda parts: 998.0)
    monkeypatch.setattr(activity._bounded, 'unavailable', lambda *args: False)
    monkeypatch.setattr(activity._external_activity, 'stamp_boards', lambda rows, *args: rows)
    source = {'id': 's', 'kanban_held': 99, 'title': 'keep'}
    expected = activity.annotate_kanban_activity.__wrapped__([deepcopy(source)])[0]
    patch = {key: value for key, value in expected.items()
             if key in annotations.FIELDS['kanban'] or key == 'execution_observations'}
    publish(store, scope, 's', 'kanban', patch)
    monkeypatch.setattr(activity, '_annotation_snapshot', forbidden)
    with annotations.use(consumer), no_io(monkeypatch):
        assert activity.annotate_kanban_activity([deepcopy(source)])[0] == expected


def test_missing_authorized_binding_never_falls_back_while_facts_active(setup, monkeypatch):
    with no_io(monkeypatch), pytest.raises(annotations.AnnotationUnavailable, match='binding missing'):
        annotations.annotate('goal', [{'id': 's'}], forbidden)
    store, scope, *_ = setup
    store.rollback(scope)
    assert annotations.annotate('goal', [], lambda rows: rows) == []


def test_rollback_closes_subscription_and_starts_legacy_once(setup):
    store, scope, consumer, _, lifecycle = setup
    assert len(store._subscribers) == 1
    store.rollback(scope)
    store.rollback(scope)
    assert lifecycle == ['stop', 'start']
    assert len(store._subscribers) == 0
    row = {'id': 's'}
    assert consumer.annotate('goal', [row], lambda rows: rows) == [row]
    store.activate(scope)
    assert lifecycle == ['stop', 'start', 'stop']
    assert len(store._subscribers) == 1


@pytest.mark.parametrize('family,module,name', [
    ('goal', 'goal_session_activity', 'annotate_goal_activity'),
    ('cron', 'cron_session_activity', 'annotate_cron_activity'),
    ('background', 'process_session_activity', 'annotate_background_activity'),
    ('turn', 'turn_session_activity', 'annotate_turn_activity'),
    ('subagent', 'subagent_session_activity', 'annotate_subagent_activity'),
    ('review', 'review_session_activity', 'annotate_review_wait'),
])
def test_nonempty_canonical_family_parity(setup, monkeypatch, family, module, name):
    """Serializer parity from controlled source inputs, not producer-capture proof."""
    import importlib
    from copy import deepcopy
    from hermes_cli import kanban_execution_observations as observations

    implementation = importlib.import_module('hermes_cli.' + module)
    entry = getattr(implementation, name)
    store, scope, consumer, *_ = setup
    monkeypatch.setattr(observations, 'local_scope', lambda: ('gateway', 'default'))
    monkeypatch.setattr(annotations.time, 'time', lambda: 1000.0)
    sources = {
        'goal': {'active_goal_sessions': lambda: {'ancestor'}},
        'cron': {'cron_owner_sessions': lambda: {'ancestor'}},
        'turn': {'live_turn_session_ids': lambda: {'ancestor'}, '_leases': {}},
        'background': {
            '_owned_processes': lambda: {'ancestor': {'p2', 'p1'}},
            '_details': {
                'p1': {'id': 'p1', 'command': 'first', 'started_at': 900.0, 'pid': 123},
                'p2': {'id': 'p2', 'command': 'second', 'started_at': None, 'pid': None}},
            '_evidence': {}},
        'subagent': {
            'local_scope': lambda: ('gateway', 'default'),
            '_profile_paths': lambda: [('default', 'fixture')],
            '_children': lambda *args: [('child', 'ancestor', 999.0, 900.0)]},
        'review': {
            '_row_prs': lambda rows: {0: [('owner', 'repo', 17)]},
            '_pr_info': lambda key: {
                'open': True, 'review_required': True, 'viewer': 'reviewer',
                'author': 'author', 'requested': ['user:reviewer'], 'requested_at': 900.0}},
    }
    for attribute, value in sources[family].items():
        monkeypatch.setattr(implementation, attribute, value)
    source = {'id': 's', 'profile': 'default', '_lineage_ids': ['ancestor'], 'title': 'keep'}
    expected = entry.__wrapped__([deepcopy(source)])[0]
    patch = {key: value for key, value in expected.items()
             if key in annotations.FIELDS[family] or key == 'execution_observations'}
    assert patch  # A vacuous empty serializer comparison is not parity coverage.
    publish(store, scope, 's', family, patch)
    values = sources[family]
    pure_rows = [deepcopy(source)]
    projectors = {
        'goal': lambda: implementation.project_goal_activity(pure_rows, {'ancestor'}),
        'cron': lambda: implementation.project_cron_activity(pure_rows, {'ancestor'}),
        'turn': lambda: implementation.project_turn_activity(
            pure_rows, live={'ancestor'}, leases={}, gateway='gateway', now=1000.0),
        'background': lambda: implementation.project_background_activity(
            pure_rows, owned=values['_owned_processes'](), details=values['_details'], evidence={},
            gateway='gateway', local_profile='default', now=1000.0),
        'subagent': lambda: implementation.project_subagent_activity(
            pure_rows, by_profile={'default': (values['_children'](), None)}, gateway='gateway', now=1000.0),
        'review': lambda: implementation.project_review_row(
            pure_rows[0], reviews=[(('owner', 'repo', 17), values['_pr_info'](None))], now=1000.0),
    }
    with annotations.use(consumer), no_io(monkeypatch):
        projectors[family]()
        assert pure_rows[0] == expected
        assert entry([deepcopy(source)])[0] == expected
