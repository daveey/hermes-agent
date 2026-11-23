"""Exact event marks should not run two aggregates over each history row."""
import sqlite3

import pytest

from plugins.kanban.dashboard import board_read as br


def test_event_marks_exact_and_narrow_vm_work():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT)')
        conn.execute('CREATE INDEX events_task ON task_events(task_id)')
        conn.executemany('INSERT INTO task_events(task_id) VALUES (?)', [('a',)] * 10000)
        conn.execute("INSERT INTO task_events(id,task_id) VALUES (20000,'b')")
        steps = []
        conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
        assert br._event_marks(conn, ['a', 'b', 'missing']) == {'a': (10000, 10000), 'b': (1, 20000)}
        conn.set_progress_handler(None, 0)
        assert len(steps) < 500, 'use covering COUNT plus MAX seek, not grouped COUNT/MAX scan'
        conn.execute('DELETE FROM task_events WHERE id=10000')
        assert br._event_marks(conn, ['a']) == {'a': (9999, 9999)}
        conn.execute("UPDATE task_events SET task_id='b' WHERE id=9999")
        assert br._event_marks(conn, ['a', 'b']) == {'a': (9998, 9998), 'b': (2, 20000)}
        assert br._event_marks(conn, []) == {}
        assert br._event_marks(conn, ['missing']) == {}


HISTORY = 30000


def _history_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE tasks (id TEXT, title TEXT);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, created_at INTEGER);"
        "CREATE INDEX idx_events_task ON task_events(task_id, created_at);"
        "CREATE INDEX idx_events_kind_task_id ON task_events(kind, task_id, id);"
        "CREATE TABLE task_runs (id INTEGER, task_id TEXT, outcome TEXT);"
        "INSERT INTO tasks VALUES ('t', 'a'), ('u', 'b');")
    conn.executemany("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, ?, 1)",
                     [('t', 'progress')] * HISTORY + [('t', 'heartbeat')] * 3 + [('u', 'created')])
    return conn


def _refresh(conn, prev, changed, loads):
    rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()

    def load(c, task_ids):
        loads.append(tuple(task_ids))
        return None, br._rows_by_task(c, 'task_events', task_ids)
    return br._incremental_diag_inputs(conn, rows, prev, load, graphs={}, event_changed=changed)


def test_trusted_refresh_marks_only_heartbeats_the_cursor_excludes(monkeypatch):
    """P2 marks every event insert except heartbeats and every event update/delete, so a trusted
    refresh must not COUNT each displayed task's whole history to learn that nothing moved."""
    monkeypatch.setattr(br, '_wake_states', lambda *_: {})
    conn = _history_db()
    loads = []
    _, memo = _refresh(conn, {}, frozenset(), loads)
    assert loads == [('t', 'u')]
    steps = []
    conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
    inputs, memo = _refresh(conn, memo, frozenset(), loads)
    conn.set_progress_handler(None, 0)
    assert loads == [('t', 'u')], 'nothing moved: retain every history'
    assert len(steps) < 100, f'trusted marks must not scan whole histories ({len(steps)} x100 VM steps)'
    assert len(inputs[1]['t']) == HISTORY + 3
    # An unmarked heartbeat insert is still seen exactly. The fixture never commits, so every
    # refresh reads in one transaction and the first one sealed each history: the heartbeat is
    # appended to the retained rows (board_event_history.extend), never re-read whole.
    conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES ('t', 'heartbeat', 2)")
    inputs, memo = _refresh(conn, memo, frozenset(), loads)
    from plugins.kanban.dashboard.board_event_history import ORDERED_AGGREGATES
    assert loads[-1] == (('t', 'u') if ORDERED_AGGREGATES else ('t',)) and len(inputs[1]['t']) == HISTORY + 4
    assert inputs[1]['t'][-1]['kind'] == 'heartbeat' and inputs[1]['t'][-1]['created_at'] == 2
    # A heartbeat backfilled below every id (pickup history) moves the count, not the max.
    conn.execute("INSERT INTO task_events (id, task_id, kind, created_at) VALUES (-5, 'u', 'heartbeat', 0)")
    inputs, memo = _refresh(conn, memo, frozenset(), loads)
    # 'u' is shorter than board_event_history.RECENT, so its sealed head is empty: every row is
    # read again with its tail, not through a reload.
    assert loads[-1] == (('t', 'u') if ORDERED_AGGREGATES else ('u',)) and inputs[1]['u'][0]['id'] == -5
    assert len(inputs[1]['u']) == 2
    # Marked changes reload through the cursor set.
    conn.execute("DELETE FROM task_events WHERE id = 1")
    inputs, memo = _refresh(conn, memo, frozenset({'t'}), loads)
    assert loads[-1] == ('t',) and len(inputs[1]['t']) == HISTORY + 3
    conn.close()


@pytest.mark.parametrize('first', ['trusted', 'legacy'])
def test_legacy_refresh_still_counts_every_event(monkeypatch, first):
    """Without a trusted cursor, an unmarked delete of an old event (a prune) must still reload."""
    monkeypatch.setattr(br, '_wake_states', lambda *_: {})
    conn = _history_db()
    loads = []
    _, memo = _refresh(conn, {}, frozenset() if first == 'trusted' else None, loads)
    _, memo = _refresh(conn, memo, None, loads)
    if first == 'legacy':
        assert len(loads) == 1, 'an unchanged legacy refresh retains histories'
    conn.execute("DELETE FROM task_events WHERE id = 5")
    inputs, memo = _refresh(conn, memo, None, loads)
    assert loads[-1] == ('t',) and len(inputs[1]['t']) == HISTORY + 2
    conn.close()
