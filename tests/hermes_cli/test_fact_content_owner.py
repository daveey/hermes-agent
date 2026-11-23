"""Real task + session adapters through one production content owner."""
import json
import threading

import pytest

from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_content_owner import ContentBinding, ContentOwner, ContentStartupError
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_inprocess import InProcessFacts, IncrementalLifecycle
from hermes_cli.fact_materializers import Projection, view_reducer
from hermes_cli.fact_parity import ExpectedView, compare_views
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_source_commit import SourcePublisher
from hermes_cli.fact_store import FactStore, view_key
from hermes_cli.sessions_projection import assemble_sessions
from tests.hermes_cli import test_fact_task_source as tasks
from tests.hermes_cli import test_fact_session_source as sessions
from tests.waiting import wait_until

home, board = tasks.home, tasks.board
store = sessions.store


@pytest.mark.platforms('linux')
def test_native_sources_through_local_transport_and_canonical_projection(board, store):
    db, reader, path = store
    db.create_session('a', 'cli')
    db.create_session('b', 'cli')
    identity = tasks.kb.create_task(board, title='task', assignee='w')
    tasks.kb.create_task(board, title='second task forces paged FULL', assignee='w')
    board_path = next(r[2] for r in board.execute('PRAGMA database_list') if r[1] == 'main')
    scope = Scope('install', 'profile')
    task_source = SourceId(scope, 'tasks.content', 'board')
    session_source = SourceId(scope, 'sessions.content', 'local')
    facts = FactStore()
    engine = ReducerEngine(facts, scope)
    session_keys = SourcePublisher(engine, session_source, 'SessionContentInput')
    task_keys = SourcePublisher(engine, task_source, 'TaskContentInput')
    key = session_keys.key('a')
    thread_ids = set()
    def hydrate(conn, ids):
        thread_ids.add(threading.get_ident())
        return sessions.rows(conn, ids)
    def task_hydrate(conn, ids):
        thread_ids.add(threading.get_ident())
        return tasks.rows(conn, ids)
    def render(selector, inputs, now):
        return assemble_sessions([dict(inputs[key].value)], {'profile': 1}, 20, 0, [], {})
    engine.register(view_reducer([Projection('sessions', ('SessionContentInput',),
                                            lambda *args: (key,), render)]))
    engine.bind(view_key(scope, 'sessions', {}))
    local = InProcessFacts(facts, {scope}, demand=lambda *args: None)
    handle = local.acquire(scope, lifecycle=IncrementalLifecycle({(scope, 'sessions')}))
    owner = ContentOwner({scope: engine}, [
        ContentBinding(task_source, board_path, task_hydrate),
        ContentBinding(session_source, path, hydrate),
    ], budget=CaptureBudget(max_seconds=2, max_keys=1))
    try:
        assert wait_until(lambda: handle.v1.snapshot(scope).get(key) is not None, timeout=10)
        assert wait_until(lambda: handle.v1.snapshot(scope).get(task_keys.key(identity)) is not None, timeout=10)
        old = handle.v1.snapshot(scope)
        db._execute_write(lambda conn: conn.execute("UPDATE sessions SET input_tokens=37 WHERE id='a'"))
        assert wait_until(lambda: handle.v1.snapshot(scope).get(key).value['input_tokens'] == 37, timeout=10)
        snapshot = handle.v1.snapshot(scope)
        expected = assemble_sessions([dict(next(iter(sessions.rows(reader, ('a',)))))],
                                     {'profile': 1}, 20, 0, [], {})
        assert compare_views(snapshot, [ExpectedView('sessions', {}, json.dumps(expected).encode())]).passed
        assert snapshot.get(key).rev == snapshot.get(view_key(scope, 'sessions', {})).rev
        assert snapshot.get(key).rev == snapshot.get(session_keys.state_key).rev
        assert old.get(key).value['input_tokens'] != 37
        assert thread_ids == {owner.feed._thread.ident}
        assert threading.get_ident() not in thread_ids
        db.delete_session('a')
        assert wait_until(lambda: handle.v1.snapshot(scope).get(key) is None, timeout=10)
    finally:
        owner.close()
        handle.close()
        local.close()


def test_duplicate_and_unauthorized_sources_rejected_before_thread():
    scope = Scope('install', 'profile')
    source = SourceId(scope, 'tasks.content', 'board')
    binding = ContentBinding(source, '/unused', tasks.rows)
    engine = ReducerEngine(FactStore(), scope)
    with pytest.raises(ValueError, match='duplicate'):
        ContentOwner({scope: engine}, [binding, binding])
    with pytest.raises(ValueError, match='unauthorized'):
        ContentOwner({}, [binding])


def test_failed_startup_retains_exact_cleanup_owner(monkeypatch):
    from hermes_cli import fact_content_owner as module
    scope = Scope('install', 'profile')
    binding = ContentBinding(SourceId(scope, 'tasks.content', 'board'), '/unused', tasks.rows)
    class Feed:
        failed = True
        def __init__(self, *args, **kwargs):
            pass
        def register(self, adapter):
            raise RuntimeError('watch unavailable')
        def close(self):
            if self.failed:
                self.failed = False
                raise TimeoutError('capture still running')
    monkeypatch.setattr(module, 'ChangeFeed', Feed)
    with pytest.raises(ContentStartupError) as caught:
        ContentOwner({scope: ReducerEngine(FactStore(), scope)}, [binding])
    caught.value.owner.close()
