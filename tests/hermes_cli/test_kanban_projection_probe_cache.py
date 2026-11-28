"""Unchanged links probes avoid SQL; changed/unknown sources stay fresh."""
from contextlib import contextmanager
import sqlite3

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_projection_probe_cache as cache


@pytest.fixture
def board(tmp_path):
    path = tmp_path / 'board.db'
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT)')
    conn.execute('CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT)')
    conn.execute("INSERT INTO tasks VALUES ('one', 'before')")
    conn.commit()
    rev.ensure_revision(conn, rev.LOCAL_TABLES)
    yield path, conn
    conn.close()
    cache._cache.clear()


def probe(path, conn):
    @contextmanager
    def opened(_path):
        yield conn
    return rev.projection_content_key(path, rev.LOCAL_TABLES, opened)


def test_integrated_unchanged_zero_sql_and_changed_fresh(board):
    path, conn = board
    first = probe(path, conn)
    assert first[0] == 'projection'
    sql = []
    conn.set_trace_callback(sql.append)
    assert probe(path, conn) == first
    assert not sql
    conn.execute("UPDATE tasks SET title='after'")
    conn.commit()
    sql.clear()
    changed = probe(path, conn)
    assert sql and changed != first
    cache._cache.clear()
    assert probe(path, conn) == changed


@pytest.mark.parametrize('background', [False, True])
def test_links_consumers_skip_sql_and_refresh_changed_board(board, monkeypatch, background):
    from hermes_cli import kanban_session_activity as activity
    path, conn = board
    scope = ('fixture',)
    activity._reset_cache_for_tests()
    monkeypatch.setattr(activity, '_origin_scope', lambda: scope)
    monkeypatch.setattr(activity, '_board_db_paths', lambda: [path])
    monkeypatch.setattr(activity, '_links_fingerprint', lambda: probe(path, conn))
    def project():
        return [{'id': row[0], 'title': row[1]} for row in conn.execute('SELECT id,title FROM tasks')]
    monkeypatch.setattr(activity, '_project_remote_links', project)
    try:
        first = activity.remote_links_and_origin_rows()
        sql = []
        conn.set_trace_callback(sql.append)
        if background:
            activity._refresh_links_scope(scope)
        assert activity.remote_links_and_origin_rows() == first
        assert not sql
        conn.execute("UPDATE tasks SET title='consumer changed'")
        conn.commit()
        sql.clear()
        if background:
            activity._refresh_links_scope(scope)
        changed = activity.remote_links_and_origin_rows()
        assert sql and changed != first
        activity._reset_cache_for_tests()
        cache._cache.clear()
        assert activity.remote_links_and_origin_rows() == changed
    finally:
        activity._reset_cache_for_tests()


def test_unknown_schema_not_cached_and_repair_visible(board):
    path, conn = board
    conn.execute(f'DROP TRIGGER {rev.PREFIX}task_events_insert')
    conn.commit()
    probe(path, conn)
    sql = []
    conn.set_trace_callback(sql.append)
    probe(path, conn)
    assert sql and not cache._cache
    rev.ensure_revision(conn, rev.LOCAL_TABLES)
    assert probe(path, conn)[0] == 'projection'


def test_same_stat_commit_and_new_reader(board, monkeypatch):
    from hermes_cli import kanban_annotation_watch as watch
    path, conn = board
    monkeypatch.setattr(watch, '_stat', lambda p: [p, 1, 4096, 1])
    first = probe(path, conn)
    other = sqlite3.connect(path)
    try:
        other.execute("UPDATE tasks SET title='same stat'")
        other.commit()
        assert probe(path, conn) != first
        sql = []
        other.set_trace_callback(sql.append)
        assert probe(path, other) == probe(path, conn)
        assert sql
    finally:
        other.close()


def test_transaction_bypass_and_racing_commit(board):
    path, conn = board
    calls = []
    def reader(c, p, identity, tables):
        calls.append(1)
        return (len(calls),)
    conn.execute('BEGIN')
    assert cache.read(conn, path, (), (), reader) == (1,)
    assert cache.read(conn, path, (), (), reader) == (2,)
    assert not cache._cache
    conn.rollback()
    other = sqlite3.connect(path)
    def racing(c, p, identity, tables):
        value = rev._projection_covered_read(c, p, identity, tables)
        other.execute("UPDATE tasks SET title='raced'")
        other.commit()
        return value
    try:
        old = cache.read(conn, path, rev.file_stat(path)[0], rev.LOCAL_TABLES, racing)
        assert not cache._cache
        assert probe(path, conn)[3:] != old
    finally:
        other.close()
