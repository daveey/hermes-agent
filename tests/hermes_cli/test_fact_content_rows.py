"""Default scalar hydrators retain declared revision coverage, not operational state."""
import pytest

from hermes_cli.fact_capture import CaptureBudget, capture_tasks
from hermes_cli.fact_content_owner import ContentBinding, ContentOwner
from hermes_cli.fact_content_rows import task_rows, session_rows
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_session_source import capture_sessions
from hermes_cli.fact_store import FactStore
from hermes_cli.kanban_activity_revision import BOOKKEEPING_COLUMNS
from tests.hermes_cli import test_fact_task_source as tasks
from tests.hermes_cli import test_fact_session_source as sessions
from tests.waiting import wait_until

home, board = tasks.home, tasks.board
store = sessions.store
BUDGET = CaptureBudget(max_seconds=2)


def test_task_default_rows_cover_content_but_exclude_heartbeat(board):
    identity = tasks.kb.create_task(board, title='before', assignee='w')
    path = next(r[2] for r in board.execute('PRAGMA database_list') if r[1] == 'main')
    first = capture_tasks(path, None, task_rows, budget=BUDGET)
    row = first.records[0]
    columns = {r[1] for r in board.execute('PRAGMA table_info(tasks)')}
    assert set(row) == columns - BOOKKEEPING_COLUMNS['tasks']
    assert row['id'] == identity
    with tasks.kbc.write_txn(board):
        board.execute('UPDATE tasks SET last_heartbeat_at=2,worker_pid=3 WHERE id=?', (identity,))
    second = capture_tasks(path, first.after, task_rows, budget=BUDGET)
    assert second.ids == ()
    assert second.records == ()
    def interleaved(conn, ids):
        with tasks.kbc.write_txn(board):
            board.execute("UPDATE tasks SET title='after' WHERE id=?", (identity,))
        return task_rows(conn, ids)
    pinned = capture_tasks(path, None, interleaved, budget=BUDGET)
    assert pinned.records[0]['title'] == 'before'
    assert capture_tasks(path, pinned.after, task_rows, budget=BUDGET).records[0]['title'] == 'after'
    with pytest.raises(ValueError, match='transaction'):
        task_rows(board, (identity,))


def test_session_default_rows_cover_counters_and_content(store):
    db, reader, path = store
    db.create_session('a', 'cli')
    first = capture_sessions(path, None, session_rows, budget=BUDGET)
    assert set(first.records[0]) == {r[1] for r in reader.execute('PRAGMA table_info(sessions)')}
    db._execute_write(lambda conn: conn.execute("UPDATE sessions SET title='after',input_tokens=31 WHERE id='a'"))
    second = capture_sessions(path, first.after, session_rows, budget=BUDGET)
    assert second.records[0]['input_tokens'] == 31
    assert second.records[0]['title'] == 'after'
    assert first.records[0]['title'] != 'after'


@pytest.mark.platforms('linux')
def test_owner_default_binding_uses_declared_hydrator(board):
    identity = tasks.kb.create_task(board, title='native default', assignee='w')
    path = next(r[2] for r in board.execute('PRAGMA database_list') if r[1] == 'main')
    scope = Scope('install', 'profile')
    source = SourceId(scope, 'tasks.content', 'local')
    store = FactStore()
    owner = ContentOwner({scope: ReducerEngine(store, scope)}, [ContentBinding(source, path)], budget=BUDGET)
    try:
        key = owner.publisher(source).key(identity)
        assert wait_until(lambda: store.snapshot(scope).get(key) is not None, timeout=10)
        assert store.snapshot(scope).get(key).value['title'] == 'native default'
        assert 'last_heartbeat_at' not in store.snapshot(scope).get(key).value
    finally:
        owner.close()
