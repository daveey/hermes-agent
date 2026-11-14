"""Only committed foreground writes expedite annotation; savepoints retain custody."""
import sqlite3

import pytest

from hermes_cli import kanban_annotation_host as host
from hermes_cli import kanban_annotation_mutation as mutation
from hermes_cli import kanban_db_connect as db


@pytest.fixture
def world(monkeypatch):
    conn = sqlite3.connect(':memory:', isolation_level=None)
    conn.executescript('''
        CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT, title TEXT);
        CREATE TABLE task_links(parent_id TEXT, child_id TEXT);
        CREATE TABLE task_chat_links(task_id TEXT, session_id TEXT);
        INSERT INTO tasks VALUES('t1', 'running', 'one');
    ''')
    monkeypatch.setattr(db._kb, '_assert_not_delegated_child_mutation', lambda *a: None)
    monkeypatch.setattr(db, '_check_file_length_invariant', lambda *a: None)
    monkeypatch.setattr(db._local_status_wake, 'flush', lambda *a: None)
    monkeypatch.setattr(host, 'enabled', lambda: True)
    calls = []
    monkeypatch.setattr(host, 'kick', calls.append)
    yield conn, calls
    conn.close()


def test_own_commit_cues_after_commit_only(world):
    conn, calls = world
    with mutation.foreground(('home', 'profile')):
        with db.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done'")
            assert calls == []
    assert calls == [('home', 'profile')]
    assert not conn.in_transaction


def test_background_and_noop_never_cue(world):
    conn, calls = world
    with mutation.foreground(('home', 'profile')):
        with db.write_txn(conn):
            conn.execute("UPDATE tasks SET status=status")
    with db.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done'")
    with mutation.foreground(('home', 'profile')):
        with db.write_txn(conn):
            pass
    assert calls == []


@pytest.mark.parametrize('nested', [False, True])
def test_rollback_does_not_cue(world, nested):
    conn, calls = world
    with mutation.foreground(('home', 'profile')):
        if nested:
            with db.write_txn(conn):
                with pytest.raises(ValueError), db.write_txn(conn, allow_nested=True):
                    conn.execute("INSERT INTO task_links VALUES('t1', 't2')")
                    raise ValueError('rollback savepoint')
        else:
            with pytest.raises(ValueError), db.write_txn(conn):
                conn.execute("UPDATE tasks SET status='done'")
                raise ValueError('rollback transaction')
    assert calls == []


def test_nested_link_flushes_at_outer_commit_before_other_wakes(world, monkeypatch):
    conn, calls = world
    def other_wake(c):
        assert calls == [('home', 'profile')]
    monkeypatch.setattr(db._local_status_wake, 'flush', other_wake)
    with mutation.foreground(('home', 'profile')):
        with db.write_txn(conn):
            with db.write_txn(conn, allow_nested=True):
                conn.execute("INSERT INTO task_chat_links VALUES('t1', 's1')")
            assert calls == []
    assert calls == [('home', 'profile')]


def test_profile_context_restored(world):
    conn, calls = world
    with mutation.foreground(('home', 'outer')):
        with mutation.foreground(('home', 'inner')):
            with db.write_txn(conn):
                conn.execute("UPDATE tasks SET status='done'")
        with db.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running'")
    with db.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done'")
    assert calls == [('home', 'inner'), ('home', 'outer')]


def test_commit_failure_preserves_no_cue(world, monkeypatch):
    conn, calls = world
    boundary = db._execute_boundary_with_retry
    def fail_commit(c, sql):
        if sql == 'COMMIT':
            raise sqlite3.OperationalError('commit failed')
        return boundary(c, sql)
    monkeypatch.setattr(db, '_execute_boundary_with_retry', fail_commit)
    with mutation.foreground(('home', 'profile')):
        with pytest.raises(sqlite3.OperationalError), db.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done'")
    assert calls == []
    assert conn.execute('SELECT status FROM tasks').fetchone()[0] == 'running'
    assert not conn.in_transaction


def test_kill_switch_disables_recorder(world, monkeypatch):
    conn, calls = world
    monkeypatch.setattr(host, 'enabled', lambda: False)
    with mutation.foreground(('home', 'profile')):
        with db.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done'")
    assert calls == []


@pytest.mark.parametrize('sql', [
    "INSERT INTO tasks VALUES('t2', 'running', 'two')",
    "DELETE FROM tasks WHERE id='t1'",
    "INSERT INTO task_links VALUES('t1', 't2')",
    "INSERT INTO task_chat_links VALUES('t1', 's1')",
])
def test_membership_changes_cue(world, sql):
    conn, calls = world
    with mutation.foreground(('home', 'profile')):
        with db.write_txn(conn):
            conn.execute(sql)
    assert calls == [('home', 'profile')]
