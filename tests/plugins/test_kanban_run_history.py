"""Run-history retention keeps excluded bookkeeping exact, even for old grants."""
import sqlite3

import pytest

from hermes_cli import kanban_diagnostics as kd
from plugins.kanban.dashboard import board_read as br
from tests.plugins import test_kanban_board_incremental as helpers

board = helpers.board


@pytest.mark.parametrize('column', ['claim_lock', 'worker_pid', 'worker_started_at', 'last_heartbeat_at'])
def test_bookkeeping_only_run_update_matches_exact_board(board, monkeypatch, column):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_task_change as ktc
    monkeypatch.setattr(kd.time, 'time', lambda: 9100.0)
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title='old grant', assignee='worker')
        conn.execute("UPDATE tasks SET status='ready', created_at=1 WHERE id=?", (tid,))
        conn.execute('DELETE FROM task_events WHERE task_id=?', (tid,))
        rid = conn.execute("INSERT INTO task_runs (task_id,profile,status,outcome,started_at,ended_at,metadata) "
                           "VALUES (?,'worker','released','released',9000,9001,?)", (tid, '{"unused_grant":true}')).lastrowid
        conn.executemany('INSERT INTO task_events (task_id,run_id,kind,payload,created_at) VALUES (?,?,?,?,?)', [
            (tid, None, 'created', '{}', 1), (tid, rid, 'claimed', '{}', 9000),
            (tid, rid, 'released', '{"unused_grant":true}', 9001)])
        conn.commit()
        baseline = helpers._stable(helpers._read(board))
        _, cursor = ktc.changed_since(conn, None)
        assert cursor is not None
        for value in (0, None):
            conn.execute(f'UPDATE task_runs SET {column}=? WHERE id=?', (value, rid))
            conn.commit()
            changed, cursor = ktc.changed_since(conn, cursor)
            assert changed == frozenset(), 'fixture must exercise excluded bookkeeping'
            actual = helpers._stable(helpers._read(board))
            assert actual == helpers._cold(board)
            assert (actual == baseline) == (value is None)
    finally:
        conn.close()



@pytest.fixture
def source(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE tasks (id TEXT, status TEXT, created_at INTEGER, assignee TEXT);
        INSERT INTO tasks VALUES ('t', 'ready', 1, 'worker');
        CREATE TABLE task_events (id INTEGER, task_id TEXT, run_id INTEGER, kind TEXT, payload TEXT, created_at INTEGER);
        INSERT INTO task_events VALUES (1,'t',NULL,'created','{}',1);
        INSERT INTO task_events VALUES (2,'t',7,'claimed','{}',9000);
        INSERT INTO task_events VALUES (3,'t',7,'released','{"unused_grant":true}',9001);
        CREATE TABLE task_runs (id INTEGER, task_id TEXT, status TEXT, outcome TEXT, ended_at INTEGER,
          metadata TEXT, summary TEXT, error TEXT, claim_lock, claim_expires, last_heartbeat_at,
          worker_pid, worker_started_at, sync_version);
        INSERT INTO task_runs (id,task_id,status,outcome,ended_at,metadata)
          VALUES (7,'t','released','released',9001,'{"unused_grant":true}');
    ''')
    monkeypatch.setattr(br, '_wake_states', lambda *_: {})
    yield conn
    conn.close()


def inputs(conn):
    rows = conn.execute('SELECT * FROM tasks').fetchall()
    events = br._rows_by_task(conn, 'task_events', ['t'])
    runs = br._rows_by_task(conn, 'task_runs', ['t'])
    return rows, events, runs


def memo(conn):
    rows, events, runs = inputs(conn)
    return br._diag_memo(rows, events, runs, br._event_marks(conn, ['t']))


def refresh(conn, old):
    return br._incremental_diag_inputs(conn, conn.execute('SELECT * FROM tasks').fetchall(), old,
                                      lambda *_args, **_kwargs: pytest.fail('unchanged events reloaded'),
                                      graphs={}, event_changed=frozenset())


def test_unchanged_run_histories_are_not_loaded_again(source, monkeypatch):
    old = memo(source)
    def unexpected(*_):
        pytest.fail('full run histories must not be loaded for an empty trusted delta')
    monkeypatch.setattr(br, '_rows_by_task', unexpected)
    got, new = refresh(source, old)
    assert got[2]['t'][0] is old['t'][4][0]
    assert new['t'][4][0] is old['t'][4][0]


@pytest.mark.parametrize('column', ['claim_lock', 'worker_pid', 'worker_started_at', 'last_heartbeat_at'])
@pytest.mark.parametrize('value', [0, '', 99])
def test_old_grant_bookkeeping_is_fresh_and_changes_visible_diagnostics(source, column, value):
    old = memo(source)
    task = dict(source.execute('SELECT * FROM tasks').fetchone())
    _rows, events, runs = inputs(source)
    baseline = kd.compute_task_diagnostics(task, events['t'], list(runs['t']), now=9100)
    assert kd._last_ready_entry_ts(events['t'], runs['t']) == 1
    for updated in (value, None):
        source.execute(f'UPDATE task_runs SET {column} = ?', (updated,))
        got, old = refresh(source, old)
        _, fresh_events, fresh_runs = inputs(source)
        actual = kd.compute_task_diagnostics(task, got[1]['t'], list(got[2]['t']), now=9100)
        expected = kd.compute_task_diagnostics(task, fresh_events['t'], list(fresh_runs['t']), now=9100)
        assert actual == expected
        assert (actual == baseline) == (updated is None)
        assert br._raw(got[2]['t']) == br._raw(fresh_runs['t'])


def test_untrusted_run_path_reloads(source, monkeypatch):
    old = memo(source)
    loaded = []
    real = br._rows_by_task
    def load(conn, table, ids):
        loaded.append(table)
        return real(conn, table, ids)
    monkeypatch.setattr(br, '_rows_by_task', load)
    rows = source.execute('SELECT * FROM tasks').fetchall()
    br._incremental_diag_inputs(source, rows, old, lambda *_args, **_kwargs: None, graphs={})
    assert 'task_runs' in loaded


def test_unchanged_run_bookkeeping_is_compared_in_sql(source):
    """Retained runs whose ids and bookkeeping are unchanged are kept without fetching and
    comparing each run's values in Python; a bookkeeping write still reaches the run."""
    old = memo(source)
    _, old = refresh(source, old)  # first trusted refresh records the signature
    statements = []
    source.set_trace_callback(statements.append)
    got, old = refresh(source, old)
    source.set_trace_callback(None)
    overlay = [s for s in statements if 'FROM task_runs' in s and 'quote(' not in s]
    assert overlay == [], 'unchanged bookkeeping must not be re-read value by value'
    source.execute("UPDATE task_runs SET worker_pid = 42")
    got, old = refresh(source, old)
    assert got[2]['t'][0]['worker_pid'] == 42
    got, old = refresh(source, old)
    assert got[2]['t'][0]['worker_pid'] == 42


def test_retained_run_overlay_is_type_exact(source):
    """An equal-comparing value of another type (1 -> 1.0 in an untyped column) is still a change."""
    old = memo(source)
    for value in (1, 1.0, 1):
        source.execute('UPDATE task_runs SET claim_expires = ?', (value,))
        got, old = refresh(source, old)
        _, _, fresh_runs = inputs(source)
        assert type(got[2]['t'][0]['claim_expires']) is type(fresh_runs['t'][0]['claim_expires']) is type(value)
