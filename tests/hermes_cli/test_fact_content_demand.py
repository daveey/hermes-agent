"""Native source owner handles request demand without request-side reduction."""
import threading

import pytest

from hermes_cli.fact_content_owner import ContentBinding, ContentOwner
from hermes_cli.fact_contract import Fact, FactKey, Scope, SourceId, Ready
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_materializers import Projection, view_reducer
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_source_commit import SourcePublisher
from hermes_cli.fact_store import FactStore, view_key
from tests.hermes_cli import test_fact_task_source as tasks
from tests.waiting import wait_until

home, board = tasks.home, tasks.board


@pytest.mark.platforms('linux')
def test_native_demand_after_bootstrap_is_owner_only_and_idempotent(board):
    identity = tasks.kb.create_task(board, title='native', assignee='w')
    path = next(r[2] for r in board.execute('PRAGMA database_list') if r[1] == 'main')
    scope = Scope('install', 'profile')
    source = SourceId(scope, 'tasks.content', 'board')
    facts = FactStore()
    engine = ReducerEngine(facts, scope)
    key = SourcePublisher(engine, source, 'TaskContentInput').key(identity)
    calls, captures = [], []
    def hydrate(conn, ids):
        captures.append(tuple(ids))
        return tasks.rows(conn, ids)
    def render(selector, inputs, now):
        calls.append(threading.get_ident())
        return {'title': inputs[key].value['title']}
    engine.register(view_reducer([Projection('title', ('TaskContentInput',),
                                            lambda *args: (key,), render)]))
    owner = ContentOwner({scope: engine}, [ContentBinding(source, path, hydrate)])
    local = InProcessFacts(facts, {scope}, demand=owner.demand)
    handle = local.acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'title')}))
    try:
        assert wait_until(lambda: facts.snapshot(scope).get(key) is not None, timeout=5)
        before = len(captures)
        with pytest.raises(ValueError, match='unregistered projection'):
            handle.demand('foreign', {})
        with pytest.raises(ValueError, match='unauthorized'):
            owner.demand(Scope('install', 'other'), 'title', {})
        handle.demand('title', {})
        assert wait_until(lambda: isinstance(handle.v1.snapshot(scope).view('title', {}), Ready), timeout=5)
        assert calls == [owner.feed._thread.ident]
        assert len(captures) == before
        revision = facts.snapshot(scope).rev
        for _ in range(50):
            handle.demand('title', {})
        # A barrier demand proves earlier duplicate commands have drained.
        handle.demand('title', {'barrier': True})
        assert wait_until(lambda: isinstance(facts.snapshot(scope).view('title', {'barrier': True}), Ready), timeout=5)
        assert len(calls) == 2
        assert facts.snapshot(scope).rev == revision + 1
        assert len(captures) == before
    finally:
        handle.close()
        local.close()
        owner.close()
    with pytest.raises(RuntimeError, match='closed'):
        owner.demand(scope, 'title', {})


def test_demand_bounds_and_publication_retry(monkeypatch):
    from hermes_cli.fact_demand_source import DemandSource
    scope = Scope('install', 'profile')
    facts = FactStore()
    engine = ReducerEngine(facts, scope)
    engine.register(view_reducer([Projection('title', (), lambda *args: (), lambda *args: {'ok': True})]))
    source = DemandSource(engine, max_keys=2, max_selector_bytes=128)
    hints = []
    watch = source.watch(lambda: hints.append(True))
    with pytest.raises(ValueError, match='selector'):
        source.demand('title', {'x': 'x' * 200})
    source.demand('title', {})
    source.demand('title', {})
    source.demand('title', {'second': True})
    with pytest.raises(ValueError, match='capacity'):
        source.demand('title', {'third': True})
    assert len(hints) == 2
    capture = source.capture(None, None)
    original = facts.publish
    def fail(*args, **kwargs):
        raise RuntimeError('publication refused')
    monkeypatch.setattr(facts, 'publish', fail)
    with pytest.raises(RuntimeError, match='refused'):
        source.commit(capture)
    assert source.capture(None, None).key == capture.key
    assert capture.key not in engine._pending
    assert capture.key not in engine._bound
    monkeypatch.setattr(facts, 'publish', original)
    # A rejected demand cannot poison independent source or health publication.
    engine.publish(now=1)
    source.commit(capture)
    second = source.capture(capture.after, None)
    source.commit(second)
    assert isinstance(facts.snapshot(scope).view('title', {}), Ready)
    assert isinstance(facts.snapshot(scope).view('title', {'second': True}), Ready)
    assert engine._pending == set()
    watch.close()
    with pytest.raises(RuntimeError, match='closed'):
        source.demand('title', {})


@pytest.mark.parametrize('broken', ['missing', 'foreign', 'kind'])
def test_bad_dependency_demand_is_unavailable_not_source_poison(broken):
    from hermes_cli.fact_demand_source import DemandSource
    scope = Scope('install', 'profile')
    facts = FactStore()
    engine = ReducerEngine(facts, scope)
    key = FactKey(scope, 'TaskContentInput', 'task')
    def dependencies(scope, selector, snapshot):
        if selector.get('good'):
            return (key,)
        if broken == 'missing':
            return (selector['absent'],)
        if broken == 'foreign':
            return (FactKey(Scope('install', 'other'), 'TaskContentInput', 'task'),)
        return (FactKey(scope, 'UndeclaredKind', 'task'),)
    engine.register(view_reducer([Projection('title', ('TaskContentInput',), dependencies,
                                            lambda *args: {'ok': True})]))
    source = DemandSource(engine)
    source.watch(lambda: None)
    engine.publish([Fact(key, {'title': 'first'}, health='fresh')], now=1)
    source.demand('title', {})
    source.commit(source.capture(None, None))
    assert facts.snapshot(scope).get(view_key(scope, 'title', {})).health == 'unknown'
    engine.publish([Fact(key, {'title': 'next'}, health='fresh')], now=2)
    source.demand('title', {'good': True})
    source.commit(source.capture(None, None))
    assert isinstance(facts.snapshot(scope).view('title', {'good': True}), Ready)
    source.close()


def test_real_store_budget_refusal_does_not_poison_sources_or_other_demands():
    from hermes_cli.fact_demand_source import DemandSource
    scope = Scope('install', 'profile')
    facts = FactStore(fact_bytes=8192)
    engine = ReducerEngine(facts, scope)
    key = FactKey(scope, 'TaskContentInput', 'task')
    engine.register(view_reducer([Projection('size', (), lambda *args: (),
                                            lambda selector, *args: {'data': 'x' * selector['size']})]))
    source = DemandSource(engine)
    source.watch(lambda: None)
    source.demand('size', {'size': 20000})
    source.demand('size', {'size': 1})
    with pytest.raises(ValueError, match='fact_budget_exceeded'):
        source.commit(source.capture(None, None))
    assert not engine._bound and not engine._pending
    engine.publish([Fact(key, {'title': 'next'}, health='stale')], now=2)
    assert facts.snapshot(scope).get(key).health == 'stale'
    source.failure()
    source.commit(source.capture(None, None))
    assert isinstance(facts.snapshot(scope).view('size', {'size': 1}), Ready)
    assert len(source._pending) == 1
    source.close()


def test_dependency_failure_retries_when_its_read_input_changes():
    from hermes_cli.fact_demand_source import DemandSource
    scope = Scope('install', 'profile')
    engine = ReducerEngine(FactStore(), scope)
    key = FactKey(scope, 'TaskContentInput', 'task')
    def dependencies(scope, selector, snapshot):
        if snapshot.get(key) is None:
            raise ValueError('not ready')
        return (key,)
    engine.register(view_reducer([Projection('title', ('TaskContentInput',), dependencies,
                                            lambda *args: {'ok': True})]))
    source = DemandSource(engine)
    source.watch(lambda: None)
    source.demand('title', {})
    source.commit(source.capture(None, None))
    assert engine.store.snapshot(scope).get(view_key(scope, 'title', {})).health == 'unknown'
    engine.publish([Fact(key, {}, health='fresh')], now=2)
    assert isinstance(engine.store.snapshot(scope).view('title', {}), Ready)
    source.close()


def test_demand_lru_evicts_bound_views_and_never_exceeds_the_bound():
    """[daveey] Demand retention is an LRU, not a ratchet: the owner's bound set stays at ``max_keys`` however many
    distinct selectors are ever asked for, and the live working set keeps being served."""
    from hermes_cli.fact_demand_source import DemandSource
    scope = Scope('install', 'profile')
    facts = FactStore()
    engine = ReducerEngine(facts, scope)
    engine.register(view_reducer([Projection('title', (), lambda *args: (), lambda *args: {'ok': True})]))
    source = DemandSource(engine, max_keys=3, max_selector_bytes=128)
    source.watch(lambda: None)

    def drain():
        cursor = None
        while True:
            capture = source.capture(cursor, None)
            source.commit(capture)
            cursor = capture.after
            if capture.key is None and not capture.more:
                return

    keys = {n: view_key(scope, 'title', {'n': n}) for n in range(8)}
    for n in range(3):
        source.demand('title', {'n': n})
    drain()
    assert engine._bound == {keys[0], keys[1], keys[2]}
    source.demand('title', {'n': 0})  # touch: 1 is now the least recently demanded
    source.demand('title', {'n': 3})
    drain()
    assert engine._bound == {keys[0], keys[2], keys[3]}
    for n in range(4, 8):
        source.demand('title', {'n': n})
        drain()
        assert len(engine._bound) <= 3 and len(source._accepted) == 3 and not source._evicted
    assert engine._bound == {keys[5], keys[6], keys[7]}
    # An evicted selector is simply demanded again and re-bound; its fact was never deleted.
    source.demand('title', {'n': 0})
    drain()
    assert keys[0] in engine._bound and isinstance(facts.snapshot(scope).view('title', {'n': 0}), Ready)
    # Unserved demands are never evicted to make room: that would drop them silently.
    unserved = DemandSource(engine, max_keys=2, max_selector_bytes=128)
    unserved.watch(lambda: None)
    unserved.demand('title', {'u': 1})
    unserved.demand('title', {'u': 2})
    with pytest.raises(ValueError, match='capacity'):
        unserved.demand('title', {'u': 3})
