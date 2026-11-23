"""Skipped upstream cards must not acquire replicas or read dependency graphs."""
import sqlite3

import pytest

from hermes_cli import kanban_session_activity_card_details as details


@pytest.mark.parametrize('status', ['done', 'archived', 'blocked', 'triage'])
@pytest.mark.parametrize('explicit', [False, True])
def test_skipped_only_clears_stale_fields_without_reads(monkeypatch, status, explicit):
    with sqlite3.connect(':memory:') as conn, sqlite3.connect(':memory:') as replica:
        statements = []
        conn.set_trace_callback(statements.append)
        replica.set_trace_callback(statements.append)
        monkeypatch.setattr(details, '_replica_for', lambda _: pytest.fail('unneeded replica acquisition'))
        found = {'task': {'status': status, 'blocked_upstream': {'id': 'stale'}, 'upstream': {'id': 'stale'}}}
        details._add_blocked_upstream(conn, found, **({'replica': replica} if explicit else {}))
        assert found == {'task': {'status': status}}
        assert statements == []
        assert replica.execute('SELECT 1').fetchone() == (1,)


def test_mixed_roots_exclude_skipped_rows_and_preserve_pending_walk(monkeypatch):
    with sqlite3.connect(':memory:') as conn:
        conn.executescript('CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT);'
                           'CREATE TABLE task_links (child_id TEXT, parent_id TEXT);'
                           "INSERT INTO tasks VALUES ('pending', 'ready'), ('parent', 'blocked');"
                           "INSERT INTO task_links VALUES ('pending', 'parent');")
        roots = details._root_parents
        seen = []

        def record(conn, replica, ids):
            seen.append(list(ids))
            return roots(conn, replica, ids)

        monkeypatch.setattr(details, '_root_parents', record)
        found = {'skip': {'status': 'done', 'upstream': {'id': 'stale'}},
                 'pending': {'status': 'ready', 'upstream': {'id': 'stale'}}}
        details._add_blocked_upstream(conn, found, replica=None)
        assert seen == [['pending']]
        assert found == {'skip': {'status': 'done'}, 'pending': {
            'status': 'ready', 'blocked_upstream': {'id': 'parent', 'status': 'blocked'}}}


def test_skipped_fields_clear_even_when_pending_root_query_fails(monkeypatch):
    with sqlite3.connect(':memory:') as conn:
        def fail(*args):
            raise sqlite3.OperationalError('unavailable')

        monkeypatch.setattr(details, '_root_parents', fail)
        found = {'skip': {'status': 'triage', 'blocked_upstream': {'id': 'stale'}},
                 'pending': {'status': 'ready', 'upstream': {'id': 'stale'}}}
        details._add_blocked_upstream(conn, found, replica=None)
        assert found == {'skip': {'status': 'triage'}, 'pending': {'status': 'ready'}}
