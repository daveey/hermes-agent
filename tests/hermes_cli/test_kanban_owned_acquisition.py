"""Connection-owned acquisition: real SQLite scopes, snapshots and legacy parity."""
import sqlite3

import pytest

from hermes_cli import kanban_execution_source as source
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_session_activity_card_details as details
from hermes_cli import kanban_session_outcome_tasks as selection
from tests.hermes_cli import test_kanban_session_outcome_tasks as fixtures


@pytest.mark.parametrize('size', [1, 8, 64, 4097])
def test_complete_membership_pairs_fit_json_scope(tmp_path, size):
    with fixtures._connect(tmp_path / 'board.db') as conn:
        conn.row_factory = sqlite3.Row
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        ids = [f's-{n}' for n in range(size)]
        for sid in ids:
            fixtures._insert_task(conn, f't-{sid}', 'running', sid)
            fixtures._sub(conn, f't-{sid}', sid, None)
        fixtures._insert_task(conn, 'shared', 'running', ids[0])
        for sid in ids:
            fixtures._sub(conn, 'shared', sid, None)
        fixtures._insert_task(conn, 'uncounted', 'running', 'elsewhere')
        fixtures._sub(conn, 'uncounted', ids[0], 'touch')
        found, pairs = selection.select_listed_local_task_set(conn, ids, ('running',))
        assert len(found) == size + 1
        assert len(pairs) == size * 2
        assert pairs[(ids[0], 'shared')] == 'created'
        assert all(pairs[(sid, 'shared')] == 'subscribed' for sid in ids[1:])
        assert all(pairs[(sid, f't-{sid}')] == 'created' for sid in ids)
        assert selection.select_listed_local_tasks(conn, ids, ('running',)) == found


def test_owned_set_reuses_cursor_snapshot_without_any_implicit_open(tmp_path, monkeypatch):
    from hermes_cli import kanban_session_acquisition as acquisition

    path = tmp_path / 'board.db'
    with fixtures._connect(path) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        fixtures._insert_task(writer, 'card', 'running', 'scope', title='before cursor')
        writer.commit()
        with sqlite3.connect(path) as captured:
            captured.row_factory = sqlite3.Row
            captured.execute('PRAGMA query_only=ON')
            captured.execute('BEGIN')
            assert captured.execute('SELECT title FROM tasks').fetchone()[0] == 'before cursor'
            writer.execute("UPDATE tasks SET title='after cursor'")
            writer.commit()
            monkeypatch.setattr(details, '_replica_for', lambda *a: pytest.fail('implicit shadow open'))
            monkeypatch.setattr(activity, 'cached_ro', lambda *a: pytest.fail('implicit local open'))
            cards, pairs = acquisition.read_session_task_set(captured, path, ['scope'], ('running',),
                                                             observed_at=100, scope=('fixture', 'default'))
            assert cards['card']['title'] == 'before cursor'
            assert pairs == {('scope', 'card'): 'created'}
            assert captured.in_transaction
            assert captured.execute('SELECT title FROM tasks').fetchone()[0] == 'before cursor'
            assert cards['card']['_execution_source']['observed_at'] == 100


def test_explicit_replica_is_borrowed_and_none_never_opens(tmp_path, monkeypatch):
    with fixtures._connect(tmp_path / 'local.db') as conn, fixtures._connect(tmp_path / 'replica.db') as replica:
        conn.row_factory = replica.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE task_links (parent_id TEXT, child_id TEXT)')
        replica.execute('CREATE TABLE task_links (parent_id TEXT, child_id TEXT)')
        fixtures._insert_task(conn, 'child', 'todo', 's')
        conn.execute("INSERT INTO task_links VALUES ('foreign', 'child')")
        fixtures._insert_task(replica, 'foreign', 'blocked', 's')
        monkeypatch.setattr(details, '_replica_for', lambda *a: pytest.fail('implicit shadow open'))
        found = {'child': {'status': 'todo'}}
        details._add_blocked_upstream(conn, found, replica=None)
        assert 'blocked_upstream' not in found['child']
        details._add_blocked_upstream(conn, found, replica=replica)
        assert found['child']['blocked_upstream'] == {'id': 'foreign', 'status': 'blocked'}
        assert replica.execute('SELECT 1').fetchone()[0] == 1


def test_contained_statuses_use_only_explicit_parent_input(tmp_path, monkeypatch):
    from hermes_cli import kanban_contained_wait as contained

    with fixtures._connect(tmp_path / 'local.db') as conn, fixtures._connect(tmp_path / 'replica.db') as replica:
        for db in (conn, replica):
            db.execute('ALTER TABLE tasks ADD COLUMN completed_at REAL')
        fixtures._insert_task(replica, 'authority', 'done', 's', completed_at=99)
        monkeypatch.setattr(contained, '_replica_status', lambda *a: pytest.fail('implicit replica acquisition'))
        assert contained.statuses(conn, ['authority'], replica=None) == {}
        assert contained.statuses(conn, ['authority'], replica=replica) == {'authority': ('done', 99)}
        assert replica.execute('SELECT 1').fetchone()[0] == 1


def test_large_owned_set_keeps_optional_details_under_999_limit(tmp_path):
    from hermes_cli import kanban_session_acquisition as acquisition

    path = tmp_path / 'board.db'
    with fixtures._connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('ALTER TABLE task_events ADD COLUMN run_id INTEGER')
        conn.execute('ALTER TABLE task_runs ADD COLUMN ended_at REAL')
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        for n in range(1001):
            tid = f't-{n}'
            fixtures._insert_task(conn, tid, 'running', 'scope')
            conn.execute('INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?, ?, ?, ?)',
                         (tid, 'comment', '{}', 99))
            conn.execute('INSERT INTO task_runs(task_id,started_at,summary) VALUES (?, ?, ?)',
                         (tid, 98, 'kept note'))
        cards, _ = acquisition.read_session_task_set(conn, path, ['scope'], ('running',),
                                                     observed_at=100, scope=('fixture', 'default'))
        assert len(cards) == 1001
        assert all(card.get('note') == 'kept note' for card in cards.values())
        assert all(card.get('activity_kind') == 'comment' for card in cards.values())
        assert all('_execution_source' in card for card in cards.values())


def test_execution_chunk_respects_double_bound_variable_limit():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY)')
        conn.execute('CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)')
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        assert source.assemble(conn, [f'absent-{n}' for n in range(500)],
                               board='default', gateway='fixture', profile='default', observed_at=100) == {}
