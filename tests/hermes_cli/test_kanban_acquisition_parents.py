"""Captured parent graph custody, containment and local-empty precedence."""
import sqlite3

import pytest

from hermes_cli import kanban_contained_wait as contained
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_session_acquisition as acquisition
from hermes_cli import kanban_session_activity_card_details as details
from tests.hermes_cli import test_kanban_contained_strand as containment_fixtures
from tests.hermes_cli import test_kanban_session_outcome_tasks as selection_fixtures

board = containment_fixtures.board


def test_owned_containment_wait_never_discovers_an_external_reader(board, monkeypatch, tmp_path):
    relay, authority, _ = containment_fixtures._contained(board)
    board.execute("UPDATE tasks SET session_id='scope' WHERE id=?", (relay,))
    # Move the authority facts into a separately captured replica so the old
    # implicit status fallback would really be required, not vacuously skipped.
    with selection_fixtures._connect(tmp_path / 'replica.db') as replica:
        replica.execute('ALTER TABLE tasks ADD COLUMN completed_at REAL')
        selection_fixtures._insert_task(replica, authority, 'done', 'other',
                                        completed_at=containment_fixtures.NOW - 3600)
        # Parent lookup uses the missing local ID; containment events remain intact.
        board.execute('UPDATE tasks SET id=? WHERE id=?', ('moved-authority', authority))
        monkeypatch.setattr(contained, '_replica_status', lambda *a: pytest.fail('implicit status reader'))
        monkeypatch.setattr(details, '_replica_for', lambda *a: pytest.fail('implicit graph reader'))
        from pathlib import Path
        path = Path(next(row[2] for row in board.execute('PRAGMA database_list') if row[1] == 'main'))
        rows, _ = acquisition.read_session_task_set(board, path, ['scope'], ('triage',),
                                                     observed_at=containment_fixtures.NOW, scope=('fixture', 'default'))
        assert rows[relay]['wait']['contained']['authority']['status'] == 'unknown'
        rows, _ = acquisition.read_session_task_set(board, path, ['scope'], ('triage',),
                                                     observed_at=containment_fixtures.NOW, scope=('fixture', 'default'), replica=replica)
        assert rows[relay]['wait']['contained']['authority']['status'] == 'done'
        assert rows[relay]['wait']['agent_owned'] is True
        assert replica.execute('SELECT 1').fetchone()[0] == 1


@pytest.mark.parametrize('depth', [1, 3])
@pytest.mark.parametrize('local_empty', [False, True])
def test_explicit_graph_matches_legacy_with_foreign_multihop_and_local_empty(tmp_path, monkeypatch, depth, local_empty):
    with selection_fixtures._connect(tmp_path / 'local.db') as conn, selection_fixtures._connect(tmp_path / 'replica.db') as replica:
        conn.row_factory = replica.row_factory = sqlite3.Row
        for db in (conn, replica):
            db.execute('CREATE TABLE task_links (parent_id TEXT, child_id TEXT)')
            db.execute('ALTER TABLE tasks ADD COLUMN containment_episode TEXT')
            db.execute('ALTER TABLE task_events ADD COLUMN uid TEXT')
        selection_fixtures._insert_task(conn, 'child', 'todo', 'scope')
        for n in range(depth):
            child = 'child' if n == 0 else f'parent-{n - 1}'
            parent = f'parent-{n}'
            selection_fixtures._insert_task(replica, parent, 'triage' if n == depth - 1 else 'todo', 'other')
            replica.execute('INSERT INTO task_links VALUES (?,?)', (parent, child))
        if not local_empty:
            conn.execute("INSERT INTO task_links VALUES ('parent-0','child')")
        replica.execute('UPDATE tasks SET containment_episode=? WHERE id=?', ('ep', f'parent-{depth - 1}'))
        replica.execute('INSERT INTO task_events(task_id,kind,payload,created_at,uid) VALUES (?,?,?,?,?)',
                        (f'parent-{depth - 1}', 'contained', '{"policy":{"authority":{"task_id":"auth"}}}', 99, 'ep'))
        selection_fixtures._insert_task(replica, 'auth', 'running', 'other')
        # Legacy owns/ closes a second real handle, explicit input stays borrowed.
        replica.commit()
        opened = []

        def legacy_replica(_conn):
            reader = sqlite3.connect(tmp_path / 'replica.db')
            reader.row_factory = sqlite3.Row
            opened.append(reader)
            return reader

        monkeypatch.setattr(details, '_replica_for', legacy_replica)
        legacy = {'child': {'status': 'todo'}}
        details._add_blocked_upstream(conn, legacy)
        explicit = {'child': {'status': 'todo'}}
        details._add_blocked_upstream(conn, explicit, replica=replica)
        assert explicit == legacy
        if local_empty:
            assert 'blocked_upstream' not in explicit['child']
        else:
            assert explicit['child']['blocked_upstream'] == {
                'id': f'parent-{depth - 1}', 'status': 'triage',
                'contained': {'episode': 'ep', 'authority': {'id': 'auth', 'status': 'running'}}}
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[0].execute('SELECT 1')
        assert replica.execute('SELECT 1').fetchone()[0] == 1
