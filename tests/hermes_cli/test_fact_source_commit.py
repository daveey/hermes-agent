"""Source commit boundaries use real captures and atomic reducer generations."""
from dataclasses import replace

import pytest

from hermes_cli.fact_capture import CaptureBudget, capture_tasks
from hermes_cli.fact_contract import Fact, FactKey, Scope, SourceId
from hermes_cli.fact_reducers import ReducerEngine, ReducerSpec
from hermes_cli.fact_source_commit import SourcePublisher
from hermes_cli.fact_store import FactStore
from tests.hermes_cli import test_fact_task_source as helpers

home, board = helpers.home, helpers.board


def setup_source(board):
    path = next(r[2] for r in board.execute('PRAGMA database_list') if r[1] == 'main')
    scope = Scope('install', 'profile', 'board')
    store = FactStore()
    engine = ReducerEngine(store, scope)
    publisher = SourcePublisher(engine, SourceId(scope, 'tasks.content', 'local'), 'TaskContentInput')
    return path, store, engine, publisher


def capture(path, before=None):
    return capture_tasks(path, before, helpers.rows, budget=CaptureBudget(max_seconds=2))


def test_native_full_dirty_deletion_and_heartbeat(board):
    path, store, engine, publisher = setup_source(board)
    a = helpers.kb.create_task(board, title='a', assignee='w')
    b = helpers.kb.create_task(board, title='b', assignee='w')
    calls = []
    output = FactKey(engine.scope, 'Dependent', a)
    def reduce(key, snapshot, now):
        calls.append(now)
        row = snapshot.get(publisher.key(a))
        return Fact(key, row.value if row else None, health='fresh')
    engine.register(ReducerSpec('one', 1, 'Dependent', ('TaskContentInput',),
                               lambda *args: {publisher.key(a)}, reduce))
    engine.bind(output)
    first = capture(path)
    publisher.commit(first)
    pinned = store.snapshot(engine.scope)
    members = publisher._members
    assert pinned.get(output).rev == pinned.get(publisher.state_key).rev
    with helpers.kbc.write_txn(board):
        board.execute("UPDATE tasks SET title='b2' WHERE id=?", (b,))
    second = capture(path, first.after)
    publisher.commit(second)
    assert publisher._members is members  # content-only delta never copies full membership
    assert store.snapshot(engine.scope).get(publisher.membership_key) is pinned.get(publisher.membership_key)
    assert len(calls) == 1
    assert store.snapshot(engine.scope).get(publisher.key(b)).value['title'] == 'b2'
    assert pinned.get(publisher.key(b)).value['title'] == 'b'
    before = store.snapshot(engine.scope).rev
    with helpers.kbc.write_txn(board):
        board.execute('UPDATE tasks SET last_heartbeat_at=12 WHERE id=?', (a,))
    heartbeat = capture(path, second.after)
    publisher.commit(heartbeat)
    assert store.snapshot(engine.scope).rev == before
    with helpers.kbc.write_txn(board):
        board.execute('DELETE FROM tasks WHERE id=?', (a,))
    publisher.commit(capture(path, heartbeat.after))
    assert len(calls) == 2
    assert store.snapshot(engine.scope).get(publisher.key(a)) is None
    assert store.snapshot(engine.scope).get(publisher.membership_key).value == (publisher.key(b),)


def test_failed_commit_retains_cursor_membership_and_atomic_read(board, monkeypatch):
    path, store, engine, publisher = setup_source(board)
    identity = helpers.kb.create_task(board, title='a', assignee='w')
    first = capture(path)
    original = store.publish
    def fail(*args, **kwargs):
        raise RuntimeError('publication failure')
    monkeypatch.setattr(store, 'publish', fail)
    with pytest.raises(RuntimeError, match='publication failure'):
        publisher.commit(first)
    assert publisher.cursor is None
    assert store.snapshot(engine.scope).get(publisher.key(identity)) is None
    monkeypatch.setattr(store, 'publish', original)
    publisher.commit(first)
    assert publisher.cursor == first.after
    assert store.snapshot(engine.scope).get(publisher.key(identity)) is not None


def test_failure_keeps_last_good_and_empty_retry_recovers(board):
    path, store, engine, publisher = setup_source(board)
    identity = helpers.kb.create_task(board, title='a', assignee='w')
    first = capture(path)
    publisher.commit(first)
    publisher.failure()
    stale = store.snapshot(engine.scope)
    assert stale.get(publisher.key(identity)).health == 'stale'
    assert stale.get(publisher.key(identity)).value['title'] == 'a'
    assert publisher.cursor == first.after
    publisher.commit(capture(path, first.after))
    fresh = store.snapshot(engine.scope)
    assert fresh.get(publisher.key(identity)).health == 'fresh'
    assert fresh.get(publisher.state_key).health == 'fresh'
    assert stale.get(publisher.key(identity)).health == 'stale'


def test_initial_failures_remain_unknown_without_revision_churn(board):
    path, store, engine, publisher = setup_source(board)
    publisher.failure()
    revision = store.snapshot(engine.scope).rev
    publisher.failure()
    snapshot = store.snapshot(engine.scope)
    assert snapshot.get(publisher.state_key).health == 'unknown'
    assert snapshot.get(publisher.membership_key).health == 'unknown'
    assert snapshot.rev == revision
    assert publisher.cursor is None
    publisher.commit(capture(path))
    assert store.snapshot(engine.scope).get(publisher.state_key).health == 'fresh'


def test_full_replaces_only_owned_members_and_refuses_bad_capture(board):
    path, store, engine, publisher = setup_source(board)
    identity = helpers.kb.create_task(board, title='a', assignee='w')
    first = capture(path)
    publisher.commit(first)
    foreign = SourcePublisher(engine, SourceId(engine.scope, 'tasks.content', 'shadow'), 'TaskContentInput')
    foreign.commit(first)
    with pytest.raises(ValueError, match='cursor'):
        publisher.commit(first)
    with pytest.raises(ValueError, match='duplicate'):
        publisher.commit(replace(first, before=first.after, records=first.records * 2))
    with helpers.kbc.write_txn(board):
        board.execute('DELETE FROM tasks WHERE id=?', (identity,))
    full = replace(capture(path), before=first.after)
    publisher.commit(full)
    assert store.snapshot(engine.scope).get(publisher.key(identity)) is None
    assert store.snapshot(engine.scope).get(foreign.key(identity)) is not None
