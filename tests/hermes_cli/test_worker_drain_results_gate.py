"""Unsent local results hold the census at UNKNOWN (never zero)."""
import sqlite3

import pytest

from hermes_cli import machine_persistent_jobs as jobs, worker_drain_results_gate as gate
from hermes_cli.kanban_db_sync import SYNC_SCHEMA_SQL
from hermes_cli.kanban_run_end_lease import _release


def _board(tmp_path, rows=0, name='kanban.db'):
    path = tmp_path / name
    conn = sqlite3.connect(path)
    conn.executescript(SYNC_SCHEMA_SQL)
    for i in range(rows):
        conn.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (f'op{i}', 'task_runs', f'run{i}', 'upsert', '{"ended_at":1}', 'h', 1))
    conn.commit()
    conn.close()
    return path


def test_finished_run_with_pending_completion_and_released_lease_is_unknown(tmp_path):
    """(a) the completion row stays pending after the lease is gone: zero must become UNKNOWN."""
    path = _board(tmp_path, rows=1)
    state = gate.local_results_state([path])
    assert (state.state, state.pending) == (gate.PENDING, 1)
    assert gate.gate_count(0, [path]) is None


def test_drained_outbox_allows_zero_and_positive_always_passes(tmp_path):
    """(b) nothing unsent: a zero is allowed; a positive count never needs the gate."""
    path = _board(tmp_path)
    assert gate.local_results_state([path]).state == gate.CLEAR
    assert gate.gate_count(0, [path]) == 0
    dirty = _board(tmp_path, rows=2, name='b.db')
    assert gate.gate_count(3, [dirty]) == 3


@pytest.mark.parametrize('kind', ['missing', 'corrupt', 'directory'])
def test_unreadable_outbox_is_unknown(tmp_path, kind):
    """(c) an unreadable or absent board is never clear."""
    path = tmp_path / 'kanban.db'
    if kind == 'corrupt':
        path.write_bytes(b'not a database' * 100)
    elif kind == 'directory':
        path.mkdir()
    assert gate.local_results_state([path]).state == gate.UNKNOWN
    assert gate.gate_count(0, [path]) is None


def test_inventory_failure_is_unknown(monkeypatch):
    from hermes_cli import worker_account_boards
    def boom():
        raise ValueError('invalid registered board path')
    monkeypatch.setattr(worker_account_boards, 'paths', boom)
    assert gate.local_results_state().state == gate.UNKNOWN


def test_board_without_sync_tables_has_no_unsent_results(tmp_path):
    path = tmp_path / 'plain.db'
    sqlite3.connect(path).close()
    assert gate.local_results_state([path]).state == gate.CLEAR


def test_census_count_exit_uses_the_gate(tmp_path, monkeypatch):
    path = _board(tmp_path, rows=1)
    from hermes_cli import worker_account_boards
    monkeypatch.setattr(worker_account_boards, 'paths', lambda: [path])
    assert jobs.census_count(0) is None
    assert jobs.census_count(2) == 2


def test_real_release_leaves_pending_row_so_gate_holds(tmp_path, monkeypatch):
    """The real kanban_run_end_lease._release drops the Board lease with the row pending."""
    path = _board(tmp_path, rows=1)
    conn = sqlite3.connect(path)
    conn.execute('ALTER TABLE kanban_sync_outbox ADD COLUMN retry_after INTEGER')  # migration column
    conn.execute('CREATE TABLE task_runs(uid TEXT, ended_at INTEGER)')
    conn.execute("INSERT INTO task_runs VALUES ('u1', 5)")
    released = []
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(sync, 'release_lease', lambda c, t, u: released.append((t, u)))
    _release(conn, 't1', 'u1')
    conn.commit()
    conn.close()
    assert released == [('t1', 'u1')]  # lease gone, a process census would be zero
    assert gate.gate_count(0, [path]) is None
