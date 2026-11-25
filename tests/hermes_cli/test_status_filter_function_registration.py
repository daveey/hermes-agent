"""Repeated count predicates must not replace a UDF with statements still active."""
import contextlib
import sqlite3

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli.sqlite_safe_read import TrackedConnection


@pytest.fixture
def connection():
    with contextlib.closing(sqlite3.connect(':memory:', factory=TrackedConnection)) as conn:
        conn.execute('CREATE TABLE tasks(id, status, session_id, human_request, block_kind)')
        conn.executemany('INSERT INTO tasks VALUES (?, ?, ?, NULL, NULL)',
                         [('one', 'ready', 'chat'), ('two', 'ready', 'chat')])
        yield conn


def test_repeated_predicate_keeps_existing_statement_alive(connection):
    activity._status_filter(connection, activity.ACTIVE_STATUSES)
    with contextlib.closing(connection.execute('SELECT kanban_open_ask(status, block_kind, human_request) FROM tasks')) as cursor:
        assert cursor.fetchone() == (0,)
        # Replacing this already registered function raises "Error creating function" in SQLite.
        activity._status_filter(connection, activity.BLOCKED_STATUSES)
        assert cursor.fetchall() == [(0,)]


def test_registration_is_connection_local_and_follows_callback_replacement(connection, monkeypatch):
    activity._status_filter(connection, activity.ACTIVE_STATUSES)
    assert connection.execute("SELECT kanban_open_ask('ready', NULL, NULL)").fetchall() == [(0,)]
    def replacement(*_args):
        return 1
    monkeypatch.setattr(activity, 'card_is_open_human_ask', replacement)
    activity._status_filter(connection, activity.BLOCKED_STATUSES)
    assert connection.execute("SELECT kanban_open_ask('ready', NULL, NULL)").fetchall() == [(1,)]
    with contextlib.closing(sqlite3.connect(':memory:', factory=TrackedConnection)) as other:
        other.execute('CREATE TABLE tasks(status)')
        activity._status_filter(other, activity.BLOCKED_STATUSES)
        assert other.execute("SELECT kanban_open_ask('ready', NULL, NULL)").fetchall() == [(1,)]


def test_plain_connection_remains_supported():
    with contextlib.closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE tasks(status)')
        activity._status_filter(conn, activity.BLOCKED_STATUSES)
        assert conn.execute("SELECT kanban_open_ask('ready', NULL, NULL)").fetchall() == [(0,)]
