"""Warm revision probes avoid SQL without hiding commits or unknown schemas."""
import sqlite3

import pytest

from hermes_cli import kanban_annotation_probe_cache as cache
from hermes_cli import kanban_annotation_revision as rev


@pytest.fixture
def board(tmp_path):
    path = tmp_path / 'board.db'
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT)')
    conn.execute("INSERT INTO tasks VALUES ('one', 'before')")
    conn.commit()
    rev.ensure_revisions(conn)
    yield path, conn
    conn.close()
    cache._cache.clear()


def probe(path, conn):
    return cache.read(conn, path, ('test',), rev.KINDS, rev.read_revisions)


def test_unchanged_executes_no_sql_changed_matches_fresh(board):
    path, conn = board
    first = probe(path, conn)
    sql = []
    conn.set_trace_callback(sql.append)
    assert probe(path, conn) == first
    assert sql == []
    conn.execute("UPDATE tasks SET title='after'")
    conn.commit()
    sql.clear()
    changed = probe(path, conn)
    assert sql and changed != first
    assert changed == rev.read_revisions(conn, ('test',), rev.KINDS)


def test_same_stat_wal_commit_invalidates(board, monkeypatch):
    from hermes_cli import kanban_annotation_watch as watch
    path, conn = board
    # Force all metadata stable; WAL-index header must independently detect it.
    monkeypatch.setattr(watch, '_stat', lambda p: [p, 1, 4096, 1])
    first = probe(path, conn)
    conn.execute("UPDATE tasks SET title='same stat commit'")
    conn.commit()
    assert probe(path, conn) != first


def test_unknown_retries_and_repair_recovers(board):
    path, conn = board
    conn.execute('DROP TRIGGER kanban_annotation_counts_tasks_update')
    conn.commit()
    assert probe(path, conn)['counts'] is None
    sql = []
    conn.set_trace_callback(sql.append)
    assert probe(path, conn)['counts'] is None
    assert sql
    rev.ensure_revisions(conn)
    assert probe(path, conn)['counts'] is not None


def test_caller_transaction_never_reuses_or_publishes(board):
    path, conn = board
    first = probe(path, conn)
    conn.execute('BEGIN')
    conn.execute("UPDATE tasks SET title='uncommitted'")
    assert probe(path, conn) != first
    assert conn.in_transaction
    conn.rollback()
    assert probe(path, conn) == first


def test_commit_during_probe_not_published(board):
    path, conn = board
    other = sqlite3.connect(path)

    def racing(reader, identity, kinds):
        values = rev.read_revisions(reader, identity, kinds)
        other.execute("UPDATE tasks SET title='raced'")
        other.commit()
        return values

    try:
        old = cache.read(conn, path, ('test',), rev.KINDS, racing)
        assert probe(path, conn) != old
    finally:
        other.close()


def test_new_reader_never_reuses_previous_reader(board):
    path, conn = board
    first = probe(path, conn)
    other = sqlite3.connect(path)
    sql = []
    other.set_trace_callback(sql.append)
    try:
        assert probe(path, other) == first
        assert sql
    finally:
        other.close()
