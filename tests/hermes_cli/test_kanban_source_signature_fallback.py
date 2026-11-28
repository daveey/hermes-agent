"""Optional file-header capabilities must never become SQLite read requirements."""
import os
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_annotation_probe_cache as annotation
from hermes_cli import kanban_projection_probe_cache as projection
from hermes_cli import kanban_session_source_cache as session
from hermes_cli import kanban_annotation_watch as watch


@pytest.mark.parametrize('consumer', ['annotation', 'projection', 'session'])
@pytest.mark.parametrize('failure', ['missing_pread', 'header_unavailable'])
@pytest.mark.parametrize('phase', ['before', 'after'])
def test_unavailable_signature_reads_without_reuse_or_publication(tmp_path, monkeypatch, consumer, failure, phase):
    path = tmp_path / 'board.db'
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE tasks(id TEXT, session_id TEXT, status TEXT, title TEXT)')
    conn.execute("INSERT INTO tasks VALUES ('t', 's', 'running', 'old')")
    conn.commit()
    memo = session._MEMO if consumer == 'session' else globals()[consumer]._cache
    memo.clear()
    armed = False
    def fail():
        nonlocal armed
        if armed:
            return
        armed = True
        if failure == 'missing_pread':
            monkeypatch.delattr(os, 'pread')
        else:
            monkeypatch.setattr(watch, 'pread_lock_safe', lambda *args: None)
    def source(*args):
        value = conn.execute('SELECT title FROM tasks').fetchone()[0]
        if phase == 'after':
            fail()
        return {'value': value} if consumer == 'annotation' else value
    def read():
        if consumer == 'annotation':
            return annotation.read(conn, path, ('board',), [SimpleNamespace(name='counts')], source)['value']
        if consumer == 'projection':
            return projection.read(conn, path, ('board',), ('tasks',), source)
        with session.selected(conn, path, ['s'], ['running']) as (rows, _):
            return rows['t']['title']
    try:
        if phase == 'before':
            assert read() == 'old'
            assert memo
            fail()
        elif consumer == 'session':
            original = session.select_listed_local_task_set
            def selecting(*args):
                result = original(*args)
                fail()
                return result
            monkeypatch.setattr(session, 'select_listed_local_task_set', selecting)
        assert read() == 'old'
        if phase == 'after':
            assert not memo
        memo.clear()
        sql = []
        conn.set_trace_callback(sql.append)
        for title in ('new', 'newer'):
            with sqlite3.connect(path) as writer:
                writer.execute('UPDATE tasks SET title=?', (title,))
            sql.clear()
            assert read() == title
            assert any('SELECT' in statement.upper() and 'tasks' in statement for statement in sql)
            assert not memo
    finally:
        memo.clear()
        conn.close()


@pytest.mark.parametrize('failure', ['missing_pread', 'header_unavailable'])
def test_public_session_reader_keeps_sql_fallback(tmp_path, monkeypatch, failure):
    from hermes_cli import kanban_session_activity as activity
    path = tmp_path / 'board.db'
    with sqlite3.connect(path) as writer:
        writer.execute('CREATE TABLE tasks(id TEXT, session_id TEXT, status TEXT, title TEXT)')
        writer.execute("INSERT INTO tasks VALUES ('t', 's', 'running', 'title')")
    activity._reset_cache_for_tests()
    try:
        assert activity._read_session_tasks(path, ['s'], ('running',), scope=('g', 'p'))[0]['title'] == 'title'
        if failure == 'missing_pread':
            monkeypatch.delattr(os, 'pread')
        else:
            monkeypatch.setattr(watch, 'pread_lock_safe', lambda *args: None)
        assert activity._read_session_tasks(path, ['s'], ('running',), scope=('g', 'p'))[0]['title'] == 'title'
    finally:
        activity._reset_cache_for_tests()
