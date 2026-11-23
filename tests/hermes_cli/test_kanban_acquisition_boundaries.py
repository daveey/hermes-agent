"""Additional acquisition refusal/custody controls discovered by seam inspection."""
import sqlite3

import pytest

from hermes_cli import kanban_contained_wait as contained
from hermes_cli import kanban_session_outcome_tasks as selection
from tests.hermes_cli import test_kanban_session_outcome_tasks as fixtures


def test_complete_membership_does_not_swallow_subscription_operational_errors(tmp_path):
    with fixtures._connect(tmp_path / 'board.db') as conn:
        conn.row_factory = sqlite3.Row
        fixtures._insert_task(conn, 'created', 'ready', 'scope')
        fixtures._insert_task(conn, 'subscribed', 'ready', 'elsewhere')
        fixtures._sub(conn, 'subscribed', 'scope', None)

        def deny_reads(action, table, column, db, trigger):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == 'kanban_notify_subs' else sqlite3.SQLITE_OK

        conn.set_authorizer(deny_reads)
        with pytest.raises(sqlite3.DatabaseError, match='prohibited'):
            selection.select_listed_local_task_set(conn, ['scope'], ('ready',))
        # Historical callers keep their existing optional-read behavior.
        assert set(selection.select_listed_local_tasks(conn, ['scope'], ('ready',))) == {'created'}
        conn.set_authorizer(None)


def test_explicit_scope_avoids_identity_store_connection_acquisition(tmp_path, monkeypatch):
    from hermes_cli import kanban_execution_observations as observations
    from hermes_cli import kanban_session_acquisition as acquisition

    path = tmp_path / 'board.db'
    with fixtures._connect(path) as conn:
        conn.row_factory = sqlite3.Row
        fixtures._insert_task(conn, 'created', 'ready', 'scope')
        monkeypatch.setattr(observations, 'card_scope', lambda *a: pytest.fail('scope resolved during capture'))
        cards, _ = acquisition.read_session_task_set(conn, path, ['scope'], ('ready',),
                                                      observed_at=100, scope=('owner-gateway', 'owner-profile'))
        assert cards['created']['_execution_scope'] == {
            'gateway': 'owner-gateway', 'profile': 'owner-profile', 'board': 'default'}


def test_legacy_details_owns_one_replica_for_all_chunks(tmp_path, monkeypatch):
    from hermes_cli import kanban_session_activity_card_details as details

    with fixtures._connect(tmp_path / 'board.db') as conn:
        conn.row_factory = sqlite3.Row
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        opened = []

        def open_replica(_conn):
            replica = sqlite3.connect(':memory:')
            opened.append(replica)
            return replica

        monkeypatch.setattr(details, '_replica_for', open_replica)
        details._add_card_details(conn, {})
        assert not opened
        details._add_card_details(conn, {f'card-{n}': {'status': 'done'} for n in range(1100)})
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[0].execute('SELECT 1')


def test_json_membership_keeps_watch_and_counted_subscription_policy(tmp_path):
    with fixtures._connect(tmp_path / 'board.db') as conn:
        conn.row_factory = sqlite3.Row
        for tid, status, owner in [('watch', 'scheduled', 'scope'), ('touch', 'ready', 'elsewhere'),
                                    ('shepherd', 'ready', 'elsewhere'), ('creator', 'ready', 'scope')]:
            fixtures._insert_task(conn, tid, status, owner)
        fixtures._sub(conn, 'watch', 'scope', 'shepherd')
        fixtures._sub(conn, 'touch', 'scope', 'touch')
        fixtures._sub(conn, 'shepherd', 'scope', 'shepherd')
        fixtures._sub(conn, 'creator', 'scope', 'touch')
        conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('watch','scheduled',?,99)",
                      ('{"watch":true}',))
        cards, pairs = selection.select_listed_local_task_set(conn, ['scope'], ('ready', 'scheduled'))
        assert set(cards) == {'creator', 'shepherd'}
        assert pairs == {('scope', 'creator'): 'created', ('scope', 'shepherd'): 'subscribed'}
        conn.execute("UPDATE tasks SET status='running' WHERE id='watch'")
        cards, pairs = selection.select_listed_local_task_set(conn, ['scope'], ('running',))
        assert set(cards) == {'watch'} and pairs == {('scope', 'watch'): 'created'}


def test_missing_optional_subscriptions_and_pre_session_schema_are_explicit(tmp_path):
    with fixtures._connect(tmp_path / 'board.db') as conn:
        conn.row_factory = sqlite3.Row
        fixtures._insert_task(conn, 'created', 'ready', 'scope')
        conn.execute('DROP TABLE kanban_notify_subs')
        cards, pairs = selection.select_listed_local_task_set(conn, ['scope'], ('ready',))
        assert set(cards) == {'created'} and pairs == {('scope', 'created'): 'created'}
        assert selection.select_listed_local_task_set(conn, [], ()) == ({}, {})
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY)')
        assert selection.select_listed_local_task_set(conn, ['scope'], ('ready',)) == ({}, {})


def test_contained_status_scopes_fit_999_without_silent_unknowns(tmp_path):
    with fixtures._connect(tmp_path / 'local.db') as conn, fixtures._connect(tmp_path / 'replica.db') as replica:
        for db in (conn, replica):
            db.execute('ALTER TABLE tasks ADD COLUMN completed_at REAL')
            db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        ids = [f'authority-{n}' for n in range(1200)]
        for tid in ids:
            fixtures._insert_task(replica, tid, 'done', 'scope', completed_at=99)
        assert contained.statuses(conn, ids, replica=replica) == {tid: ('done', 99) for tid in ids}
