"""Real board capture to decorated native request, not canned ViewFacts."""
import sqlite3
import time

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_annotation_sources import NativeAnnotationOwner, NativeAnnotationSource
from hermes_cli.fact_annotation_views import projections, selector_for
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_materializers import view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore, view_key
from tests.hermes_cli import test_kanban_session_activity as helpers
from tests.hermes_cli.annotation_fact_processes import request_guard
from tests.waiting import wait_until

kanban_root = helpers.kanban_root


def test_real_board_capture_mutation_and_request_tripwires(kanban_root, monkeypatch):
    from hermes_cli.fact_annotation_kanban import capture
    path = kanban_root / 'kanban.db'
    helpers._make_board(path, [('one', 'running', 'root'), ('two', 'ready', 'root')])
    monkeypatch.setattr(activity, '_remote_cards', lambda *args: [])
    monkeypatch.setattr(activity, '_waiting_sources', {'fixture': lambda: {'root': {'other'}}})
    monkeypatch.setattr(activity, '_done_checks', {})
    scope = Scope('kanban-native', 'default')
    store = FactStore()
    engine = ReducerEngine(store, scope)
    engine.register(view_reducer(projections()))
    source = NativeAnnotationSource(engine, SourceId(scope, 'annotations.kanban', 'local'), [path],
        lambda budget, wall, ids: capture(budget, now=wall))
    owner = NativeAnnotationOwner({scope: engine}, [], operational=[source], budget=CaptureBudget(max_seconds=2))
    local = InProcessFacts(store, {scope}, demand=owner.demand)
    life = IncrementalLifecycle({(scope, 'annotations')})
    handle = local.acquire(scope, lifecycle=life)
    binding = annotations.AnnotationFacts(handle, scope, stop_legacy=lambda: None,
                                         start_legacy=lambda: None, view_facts=True)
    row = {'id': 'tip', '_lineage_ids': ['root', 'tip']}
    selector = selector_for(scope, row, display=True)
    key = view_key(scope, 'annotations.kanban', selector)
    def ready_count(n):
        fact = store.snapshot(scope).get(key)
        return fact and fact.health == 'fresh' and fact.value['payload'].get('kanban_active') == n
    try:
        owner.demand(scope, 'annotations.kanban', selector)
        assert wait_until(lambda: ready_count(3), timeout=10)
        life.report_ready('annotations', scope)
        life.transition('annotations', scope, 'facts', 1)
        with annotations.use(binding), request_guard():
            result = activity.annotate_kanban_activity([dict(row)], display=True)[0]
        assert result['kanban_waiting_on'] == ['one', 'other', 'two']
        before = store.snapshot(scope)
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE tasks SET status='done' WHERE id='one'")
        assert wait_until(lambda: ready_count(2), timeout=10)
        assert before.get(key).value['payload']['kanban_active'] == 3
        assert store.snapshot(scope).get(key).source_vector
        with annotations.use(binding), request_guard():
            assert activity.annotate_kanban_activity([dict(row)], display=True)[0]['kanban_active'] == 2
    finally:
        life.transition('annotations', scope, 'legacy', 2)
        binding.close()
        owner.close()
        handle.close()
        local.close()


def test_partial_board_capture_is_not_fresh_empty(kanban_root, monkeypatch):
    from hermes_cli.fact_annotation_kanban import capture
    path = kanban_root / 'kanban.db'
    helpers._make_board(path, [('one', 'running', 'root')])
    original = activity._read_board
    def partial(*args, **kwargs):
        reads, _ = original(*args, **kwargs)
        return reads, False
    monkeypatch.setattr(activity, '_read_board', partial)
    with pytest.raises(RuntimeError, match='incomplete'):
        capture(CaptureBudget(max_seconds=2), now=time.time())


def test_attention_identity_matches_legacy_across_boards(kanban_root, monkeypatch):
    from hermes_cli.fact_annotation_kanban import capture
    paths = (kanban_root / 'kanban.db', kanban_root / 'boards/other/kanban.db')
    now = time.time()
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        helpers._make_board(path, [('same', 'done', 'root')])
        with sqlite3.connect(path) as conn:
            conn.execute('ALTER TABLE tasks ADD COLUMN completed_at REAL')
            conn.execute('ALTER TABLE tasks ADD COLUMN result TEXT')
            conn.execute("ALTER TABLE tasks ADD COLUMN title TEXT DEFAULT 'done fixture'")
            conn.execute('UPDATE tasks SET completed_at=?', (now,))
    monkeypatch.setattr(activity, '_board_db_paths', lambda: list(paths))
    monkeypatch.setattr(activity, '_remote_cards', lambda *args: [])
    monkeypatch.setattr(activity, '_done_checks', {'flag': lambda card: True})
    value, _ = capture(CaptureBudget(max_seconds=2), now=now)
    assert value['parts'][10] == {'root': 1}


def test_callback_and_external_errors_remain_unavailable(kanban_root, monkeypatch):
    from hermes_cli.fact_annotation_kanban import capture
    from hermes_cli import kanban_external_watch as external
    path = kanban_root / 'kanban.db'
    helpers._make_board(path, [('one', 'running', 'root')])
    monkeypatch.setattr(activity, '_remote_cards', lambda *args: [])
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('fixture source failure')
    monkeypatch.setattr(external, 'hosts_seen', broken)
    with pytest.raises(sqlite3.OperationalError, match='fixture source'):
        capture(CaptureBudget(max_seconds=2), now=time.time())
