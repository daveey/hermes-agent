"""Real persisted inputs on the shared feed, through decorated annotation calls."""
import json
from types import SimpleNamespace
import threading
import time

import pytest

from hermes_cli import fact_annotation_capture as capture
from hermes_cli import kanban_annotation_facts as annotations
from hermes_cli.fact_annotation_sources import NativeAnnotationOwner, NativeAnnotationSource
from hermes_cli.fact_annotation_views import projections, selector_for
from hermes_cli.fact_capture import CaptureBudget, CaptureBudgetExceeded
from hermes_cli.fact_contract import FactKey, Scope, SourceId
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_materializers import view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore, view_key
from tests.hermes_cli import test_fact_session_source as helpers
from tests.hermes_cli.annotation_fact_processes import request_guard
from tests.waiting import wait_until

store = helpers.store


def empty_registry(monkeypatch):
    import tools.process_registry as registry
    monkeypatch.setattr(registry, 'process_registry', SimpleNamespace(_running={}, _lock=threading.Lock()))


def test_native_four_families_actual_mutation_and_quiet_recovery(store, tmp_path, monkeypatch):
    from hermes_cli import process_session_activity as process
    from hermes_cli import turn_session_activity as turn
    from hermes_cli import subagent_session_activity as child
    from hermes_cli import review_session_activity as review
    db, _, path = store
    now = time.time()
    db.create_session('root', 'cli')
    db.create_session('child', 'subagent')
    db._execute_write(lambda conn: conn.execute(
        "UPDATE sessions SET parent_session_id='root',started_at=?,last_activity_at=? WHERE id='child'", (now, now)))
    db._execute_write(lambda conn: conn.execute(
        'INSERT INTO session_turn_leases(conversation_id,holder,acquired_at,expires_at) VALUES (?,?,?,?)',
        ('root', 'opaque-live-holder', now, now + 60)))
    db._execute_write(lambda conn: conn.execute(
        "INSERT INTO messages(session_id,role,content,timestamp) VALUES ('root','assistant',?,?)",
        ('https://github.com/owner/repo/pull/17', now)))
    checkpoint = tmp_path / 'processes.json'
    checkpoint.write_text(json.dumps([{'session_id': 'proc', 'parent_session_id': 'root', 'pid': 123,
                                       'host_start_time': 11, 'command': 'bounded fixture'}]))
    empty_registry(monkeypatch)
    monkeypatch.setattr(process, '_live', lambda pid, start: pid == 123 and start == 11)
    monkeypatch.setattr(review, '_pr_info', lambda key: {'open': True, 'review_required': True,
        'viewer': 'reviewer', 'author': 'author', 'requested': ['user:reviewer'], 'requested_at': now})
    scope = Scope('native-four', 'default')
    facts = FactStore()
    engine = ReducerEngine(facts, scope)
    engine.register(view_reducer(projections()))
    funcs = {'background': capture.processes, 'turn': capture.turns, 'subagent': capture.children}
    sources = []
    for family, fn in funcs.items():
        p = checkpoint if family == 'background' else path
        def read(budget, wall, ids, fn=fn, p=p):
            return fn(p, budget, profile='default', gateway='gateway', now=wall)
        sources.append(NativeAnnotationSource(engine, SourceId(scope, 'annotations.' + family, 'local'), [p], read))
    review_source = NativeAnnotationSource(engine, SourceId(scope, 'annotations.review', 'local'), [path],
        lambda budget, wall, ids: capture.reviews(path, budget, ids=ids, now=wall))
    sources.append(review_source)
    owner = NativeAnnotationOwner({scope: engine}, [], operational=sources, budget=CaptureBudget(max_seconds=2))
    local = InProcessFacts(facts, {scope}, demand=owner.demand)
    life = IncrementalLifecycle({(scope, 'annotations')})
    handle = local.acquire(scope, lifecycle=life)
    consumer = annotations.AnnotationFacts(handle, scope, stop_legacy=lambda: None,
                                          start_legacy=lambda: None, view_facts=True)
    row = {'id': 's', '_lineage_ids': ['root']}
    selector = selector_for(scope, row)
    entries = {'background': process.annotate_background_activity, 'turn': turn.annotate_turn_activity,
               'subagent': child.annotate_subagent_activity, 'review': review.annotate_review_wait}
    keys = {family: view_key(scope, 'annotations.' + family, selector) for family in entries}
    def ready():
        return all((f := facts.snapshot(scope).get(key)) is not None and f.health == 'fresh' for key in keys.values())
    try:
        life.report_ready('annotations', scope)
        life.transition('annotations', scope, 'facts', 1)
        with annotations.use(consumer), request_guard():
            for entry in entries.values():
                with pytest.raises(annotations.AnnotationUnavailable):
                    entry([dict(row)])
        assert wait_until(ready, timeout=10)
        with annotations.use(consumer), request_guard():
            initial = {family: entry([dict(row)])[0] for family, entry in entries.items()}
        assert initial['background']['background_active'] == 1
        assert initial['background']['execution_observations']
        assert initial['turn']['turn_live'] is True
        assert initial['turn']['execution_observations']
        assert initial['subagent']['execution_observations']
        assert initial['review']['review_wait'] == 'stuck'
        before = facts.snapshot(scope)
        checkpoint.write_text('{broken')
        assert wait_until(lambda: facts.snapshot(scope).get(keys['background']).health == 'stale', timeout=10)
        with annotations.use(consumer), pytest.raises(annotations.AnnotationUnavailable):
            process.annotate_background_activity([dict(row)])
        checkpoint.write_text('[]')
        db._execute_write(lambda conn: conn.execute("UPDATE sessions SET ended_at=? WHERE id='child'", (now,)))
        db._execute_write(lambda conn: conn.execute("DELETE FROM session_turn_leases WHERE conversation_id='root'"))
        db._execute_write(lambda conn: conn.execute(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES ('root','assistant','no PR',?)", (now + 1,)))
        def cleared():
            snap = facts.snapshot(scope)
            return ready() and not snap.get(keys['background']).value['payload'].get('background_active') and not (
                snap.get(keys['turn']).value['payload'].get('turn_live')) and not (
                snap.get(keys['subagent']).value['payload'].get('execution_observations')) and not (
                snap.get(keys['review']).value['payload'].get('review_wait'))
        assert wait_until(cleared, timeout=10)
        assert before.get(keys['turn']).value['payload']['turn_live'] is True
        assert facts.snapshot(scope).get(FactKey(scope, 'AnnotationInput', 'review')).source_vector
        life.transition('annotations', scope, 'legacy', 2)
    finally:
        consumer.close()
        owner.close()
        handle.close()
        local.close()


def test_real_capture_bounds_and_no_create_on_missing(store, tmp_path, monkeypatch):
    db, _, path = store
    empty_registry(monkeypatch)
    budget = CaptureBudget(max_seconds=2)
    absent = tmp_path / 'missing.db'
    import sqlite3
    with pytest.raises(sqlite3.Error):
        capture.children(absent, budget, profile='default', gateway='gateway', now=time.time())
    assert not absent.exists()
    bad = tmp_path / 'processes.json'
    bad.write_text('[' + ' ' * 200 + ']')
    with pytest.raises(CaptureBudgetExceeded):
        capture.processes(bad, CaptureBudget(max_bytes=100), profile='default', gateway='gateway', now=time.time())
    db.create_session('p', 'cli')
    db.create_session('a', 'subagent')
    db.create_session('b', 'subagent')
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET parent_session_id='p' WHERE id IN ('a','b')"))
    with pytest.raises(CaptureBudgetExceeded):
        capture.children(path, CaptureBudget(max_keys=1, max_seconds=2), profile='default', gateway='gateway', now=time.time())
