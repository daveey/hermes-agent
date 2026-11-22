"""Task-wide invalidations must not discard identical diagnostic histories."""
import sqlite3

import pytest

from plugins.kanban.dashboard import board_read, board_rows
from tests.plugins import test_kanban_board_incremental as helpers
from hermes_cli import kanban_db_connect as kbc

board = helpers.board


def test_content_only_edit_retains_history_and_visible_payload(board, monkeypatch):
    ids, _run = helpers._seed()
    helpers._read(board)
    loads = []
    original = board._diagnostic_inputs
    monkeypatch.setattr(board, '_diagnostic_inputs',
                        lambda c, task_ids=None: loads.append(task_ids) or original(c, task_ids))
    conn = kbc.connect()
    try:
        conn.execute('UPDATE tasks SET title = ? WHERE id = ?', ('new title', ids[0]))
        conn.commit()
    finally:
        conn.close()
    warm = helpers._stable(helpers._read(board))
    assert loads == [], 'task-only changes must retain identical event histories and reductions'
    board_rows.clear()
    assert warm == helpers._stable(helpers._read(board))
    assert any(t['title'] == 'new title' for col in warm['columns'] for t in col['tasks'])


@pytest.mark.parametrize('mutation', ['append', 'delete'])
def test_changed_event_mark_skips_exact_scan(board, monkeypatch, mutation):
    ids, _run = helpers._seed()
    before = helpers._stable(helpers._read(board))
    compared = []
    original = board_read._same_events
    monkeypatch.setattr(board_read, '_same_events',
                        lambda c, tid, history: compared.append(tid) or original(c, tid, history))
    conn = kbc.connect()
    try:
        if mutation == 'append':
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'test', '{}', 1)",
                         (ids[0],))
        else:
            conn.execute('DELETE FROM task_events WHERE task_id = ?', (ids[0],))
        conn.commit()
    finally:
        conn.close()
    warm = helpers._stable(helpers._read(board))
    assert ids[0] not in compared, 'known-changed histories must not pay an exact scan before reload'
    assert warm != before
    board_rows.clear()
    assert warm == helpers._stable(helpers._read(board))


@pytest.mark.parametrize('mutation', [
    "UPDATE task_events SET payload = 1.0 WHERE id = 1",
    "UPDATE task_events SET payload = NULL WHERE id = 1",
    "UPDATE task_events SET payload = x'31' WHERE id = 1",
    "UPDATE task_events SET payload = '1' WHERE id = 1",
    "DELETE FROM task_events WHERE id = 1",
    "INSERT INTO task_events VALUES (0, 't', 'heartbeat', 1)",
    "INSERT OR REPLACE INTO task_events VALUES (1, 't', 'edited', 1)",
    "ALTER TABLE task_events ADD COLUMN extra TEXT",
    "ALTER TABLE task_events RENAME COLUMN payload TO renamed",
])
def test_exact_history_comparison_rejects_mutations(mutation):
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload)')
        conn.execute("INSERT INTO task_events VALUES (1, 't', 'created', 1)")
        history = board_read._rows_by_task(conn, 'task_events', ['t'])['t']
        assert board_read._same_events(conn, 't', history)
        assert board_read._same_event_histories(conn, {'t': history}) == {'t': True}
        conn.execute(mutation)
        assert not board_read._same_events(conn, 't', history)
        assert board_read._same_event_histories(conn, {'t': history}) == {'t': False}
        assert conn.row_factory is sqlite3.Row


def test_exact_empty_history_and_independent_task():
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, payload)')
        assert board_read._same_events(conn, 't', ())
        assert board_read._same_event_histories(conn, {'t': (), 'other': ()}) == {'t': True, 'other': True}
        conn.execute("INSERT INTO task_events VALUES (1, 'other', 'x')")
        assert board_read._same_events(conn, 't', ())
        assert board_read._same_event_histories(conn, {'t': (), 'other': ()}) == {'t': True, 'other': False}
        conn.execute("INSERT INTO task_events VALUES (2, 't', 'x')")
        assert not board_read._same_events(conn, 't', ())

