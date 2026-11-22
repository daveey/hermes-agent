"""Exact-history checks use one query, with the old scalar implementation as oracle."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as kbc
from plugins.kanban.dashboard import board_read, board_rows
from tests.plugins import test_kanban_board_incremental as helpers

board = helpers.board


def test_title_edits_batch_exact_history_reads(board, monkeypatch):
    ids, _ = helpers._seed()
    statements = []
    from hermes_cli import kanban_generation
    original = kanban_generation.open_ro

    def observe(*args, **kwargs):
        conn = original(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(kanban_generation, 'open_ro', observe)
    helpers._read(board)
    conn = kbc.connect()
    try:
        conn.execute("UPDATE tasks SET title = title || ' edited'")
        conn.commit()
    finally:
        conn.close()
    statements.clear()
    helpers._read(board)
    scans = [s for s in statements if s.startswith('SELECT * FROM task_events WHERE')]
    assert len(scans) == 1, scans
    assert ' IN (' in scans[0]
    assert len(ids) > 1


@pytest.mark.parametrize('mutation', ['title', 'event', 'heartbeat', 'none'])
def test_payload_bytes_match_scalar_oracle(board, monkeypatch, mutation):
    ids, _ = helpers._seed()
    helpers._read(board)
    conn = kbc.connect()
    try:
        if mutation == 'title':
            conn.execute("UPDATE tasks SET title = title || ' edited'")
        elif mutation == 'event':
            conn.execute("UPDATE task_events SET payload = '{}' WHERE task_id = ?", (ids[0],))
        elif mutation == 'heartbeat':
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'heartbeat', '{}', 1)", (ids[0],))
        conn.commit()
    finally:
        conn.close()
    # Save and restore the exact pre-refresh retained rows so both implementations
    # see the same cursor and snapshot, rather than comparing two warm cache hits.
    entries = board_rows._entries.copy()
    new = json.dumps(helpers._stable(helpers._read(board)))
    board_rows._entries.clear()
    board_rows._entries.update(entries)

    def scalar(conn, histories):
        return {tid: board_read._same_events(conn, tid, history) for tid, history in histories.items()}

    monkeypatch.setattr(board_read, '_same_event_histories', scalar)
    old = json.dumps(helpers._stable(helpers._read(board)))
    assert new == old


def test_batch_empty_and_sql_error_close_cursor():
    class Cursor:
        row_factory = sqlite3.Row
        closed = False

        def execute(self, *_):
            raise sqlite3.OperationalError('injected read failure')

        def close(self):
            self.closed = True

    class Connection:
        def __init__(self):
            self.cur = Cursor()

        def cursor(self):
            return self.cur

    conn = Connection()
    assert board_read._same_event_histories(conn, {}) == {}
    with pytest.raises(sqlite3.OperationalError, match='injected'):
        board_read._same_event_histories(conn, {'t': ()})
    assert conn.cur.closed


def test_batch_orders_interleaved_history_by_id_not_creation_time():
    with sqlite3.connect(':memory:') as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE task_events (id INTEGER PRIMARY KEY, payload, task_id TEXT, created_at)')
        conn.execute('CREATE INDEX events_task ON task_events(task_id, created_at)')
        conn.executemany('INSERT INTO task_events VALUES (?, ?, ?, ?)', [
            (4, 'late', 'b', 1), (1, 1, 'a', 9), (3, 1.0, 'a', 0), (2, None, 'b', 8)])
        histories = board_read._rows_by_task(conn, 'task_events', ['b', 'a', 'missing'])
        assert board_read._same_event_histories(conn, histories) == {
            tid: board_read._same_events(conn, tid, h) for tid, h in histories.items()}
        assert all(board_read._same_event_histories(conn, histories).values())
        conn.execute("UPDATE task_events SET payload = x'31' WHERE id = 3")
        assert board_read._same_event_histories(conn, histories) == {'b': True, 'a': False, 'missing': True}
