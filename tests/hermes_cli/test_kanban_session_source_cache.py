"""Source reuse does not freeze fresh authority or caller snapshots."""
from contextlib import contextmanager
import sqlite3

import pytest

from hermes_cli import kanban_session_source_cache as cache


@pytest.fixture
def board(tmp_path):
    path = tmp_path / 'board.db'
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE tasks(id TEXT, session_id TEXT, status TEXT, title TEXT)')
    conn.execute("INSERT INTO tasks VALUES ('t', 's', 'running', 'old')")
    conn.commit()
    cache._MEMO.clear()
    yield path, conn
    cache._MEMO.clear()
    conn.close()


def read(board, ids=('s',), statuses=('running',)):
    path, conn = board
    with cache.selected(conn, path, ids, statuses) as (rows, at):
        return rows, at


def test_warm_no_selection_sql_changed_matches_fresh(board):
    path, conn = board
    first, _ = read(board)
    sql = []
    conn.set_trace_callback(sql.append)
    warm, _ = read(board)
    assert warm == first
    assert not any('tasks' in s.lower() for s in sql), sql
    warm['t']['title'] = 'caller mutation'
    assert read(board)[0] == first
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE tasks SET title='new'")
    sql.clear()
    changed = read(board)[0]
    assert any('tasks' in s.lower() for s in sql)
    cache._MEMO.clear()
    assert changed == read(board)[0]
    assert changed['t']['title'] == 'new'
    assert read(board, ids=('other',))[0] == {}
    assert read(board, statuses=('done',))[0] == {}


def test_enrichment_runs_each_pass(board, monkeypatch):
    from hermes_cli import kanban_session_activity as activity
    from hermes_cli import kanban_session_acquisition as acquisition
    path, conn = board
    @contextmanager
    def opened(_path):
        yield conn
    monkeypatch.setattr(activity, 'cached_ro', opened)
    clocks = []
    def enrich(_conn, _path, found, **kwargs):
        clocks.append(kwargs['observed_at'])
        found['t']['fresh'] = len(clocks)
    monkeypatch.setattr(acquisition, 'enrich_session_tasks', enrich)
    assert activity._read_session_tasks(path, ['s'], ('running',), scope=('g', 'p'))[0]['fresh'] == 1
    assert activity._read_session_tasks(path, ['s'], ('running',), scope=('g', 'p'))[0]['fresh'] == 2
    assert len(clocks) == 2


def test_caller_transaction_is_never_cached(board):
    _, conn = board
    read(board)
    conn.execute("UPDATE tasks SET title='private'")
    assert read(board)[0]['t']['title'] == 'private'
    conn.rollback()
    assert read(board)[0]['t']['title'] == 'old'


def test_commit_during_snapshot_not_certified(board, monkeypatch):
    path, _ = board
    original = cache.select_listed_local_task_set
    def racing(*args, **kwargs):
        result = original(*args, **kwargs)
        with sqlite3.connect(path) as writer:
            writer.execute("UPDATE tasks SET title='raced'")
        return result
    monkeypatch.setattr(cache, 'select_listed_local_task_set', racing)
    assert read(board)[0]['t']['title'] == 'old'
    assert not cache._MEMO
    monkeypatch.setattr(cache, 'select_listed_local_task_set', original)
    assert read(board)[0]['t']['title'] == 'raced'


def test_commit_between_token_and_snapshot_rejects_warm_rows(board, monkeypatch):
    path, _ = board
    read(board)
    original = cache.transaction
    @contextmanager
    def racing(conn):
        with sqlite3.connect(path) as writer:
            writer.execute("UPDATE tasks SET title='before anchor'")
        with original(conn) as at:
            yield at
    monkeypatch.setattr(cache, 'transaction', racing)
    assert read(board)[0]['t']['title'] == 'before anchor'


def test_optional_failure_retries_without_new_commit(board, monkeypatch):
    original = cache.select_listed_local_task_set
    def failed(*args, **kwargs):
        raise sqlite3.OperationalError('transient subscription error')
    monkeypatch.setattr(cache, 'select_listed_local_task_set', failed)
    assert read(board)[0]['t']['title'] == 'old'
    assert not cache._MEMO
    monkeypatch.setattr(cache, 'select_listed_local_task_set', original)
    assert read(board)[0]['t']['title'] == 'old'
    assert cache._MEMO


def test_pre_session_schema_and_bound(board, monkeypatch):
    _, conn = board
    monkeypatch.setattr(cache, '_MAX_ENTRIES', 2)
    for sid in ('s', 'a', 'b'):
        read(board, ids=(sid,))
    assert len(cache._MEMO) == 2
    monkeypatch.setattr(cache, '_MAX_CHARS', 0)
    cache._MEMO.clear()
    read(board)
    assert not cache._MEMO
    conn.execute('DROP TABLE tasks')
    conn.execute('CREATE TABLE tasks(id TEXT)')
    conn.commit()
    assert read(board)[0] is None
