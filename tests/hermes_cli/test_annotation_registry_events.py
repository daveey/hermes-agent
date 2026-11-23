"""Registry-only lifecycle hints traverse the actual native annotation owner."""
from contextlib import nullcontext
import threading
import time

from hermes_cli import fact_annotation_capture as capture
from hermes_cli.fact_annotation_sources import NativeAnnotationOwner, NativeAnnotationSource
from hermes_cli.fact_annotation_views import projections, selector_for
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_materializers import view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore, view_key
from tests.waiting import wait_until


def test_registry_only_start_and_exit_wake_real_feed(tmp_path, monkeypatch):
    from tools import process_registry as processes
    from tools.process_registry import ProcessRegistry, ProcessSession
    registry = ProcessRegistry()
    monkeypatch.setattr(processes, 'process_registry', registry)
    # Disable only file persistence; retain actual registration/finish and the
    # lifecycle publication before the writer. No file hint can mask the seam.
    monkeypatch.setattr(registry, '_checkpoint_file_lock', nullcontext)
    monkeypatch.setattr(registry, '_write_checkpoint_locked', lambda entries: None)
    monkeypatch.setattr(registry, '_completion_obligation', lambda session: None)
    monkeypatch.setattr(processes, 'save_completed_result', lambda *args: None)
    path = tmp_path / 'absent-processes.json'
    scope = Scope('registry-only', 'default')
    store = FactStore()
    engine = ReducerEngine(store, scope)
    engine.register(view_reducer(projections()))
    calls = []
    def read(budget, wall, ids):
        calls.append(wall)
        value, _ = capture.processes(path, budget, profile='default', gateway='g', now=wall)
        return value, wall + 3600
    source = NativeAnnotationSource(engine, SourceId(scope, 'annotations.background', 'local'), [path], read)
    owner = NativeAnnotationOwner({scope: engine}, [], operational=[source], budget=CaptureBudget(max_seconds=2))
    selector = selector_for(scope, {'id': 'parent'})
    key = view_key(scope, 'annotations.background', selector)
    def count():
        fact = store.snapshot(scope).get(key)
        return fact.value['payload'].get('background_active', 0) if fact and fact.health == 'fresh' else -1
    done = threading.Event()
    session = ProcessSession(id='in-memory', command='fixture', session_key='parent',
                             pid_scope='sandbox', parent_session_id='parent')
    try:
        owner.demand(scope, 'annotations.background', selector)
        assert wait_until(lambda: count() == 0, timeout=5)
        registry._track_started(session, lambda session: done.wait(5), 'annotation-fixture-reader')
        assert wait_until(lambda: count() == 1, timeout=2), (count(), calls)
        # Actual finalization, not manually editing the source or waking feed.
        registry._finish_exited(session, 0)
        assert wait_until(lambda: count() == 0, timeout=2), (count(), calls)
        assert not path.exists()
    finally:
        done.set()
        if session._reader_thread is not None:
            session._reader_thread.join(5)
        owner.close()


def test_registry_hint_scope_disposal_and_callback_failure():
    from tools.process_registry_events import subscribe, publish
    a, b = object(), object()
    calls = []
    def broken():
        raise RuntimeError('fixture callback failure')
    close_bad = subscribe(a, broken)
    close_a = subscribe(a, lambda: calls.append('a'))
    close_b = subscribe(b, lambda: calls.append('b'))
    try:
        publish(a)
        assert calls == ['a']
        close_a()
        close_a()
        publish(a)
        assert calls == ['a']
        publish(b)
        assert calls == ['a', 'b']
    finally:
        close_bad()
        close_a()
        close_b()
