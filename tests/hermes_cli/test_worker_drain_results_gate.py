"""Unsent local results hold the census at UNKNOWN (never zero)."""
import sqlite3

import pytest

from hermes_cli import machine_persistent_jobs as jobs, worker_drain_results_gate as gate
from hermes_cli.kanban_db_sync import SYNC_SCHEMA_SQL


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
    from hermes_cli import kanban_db as kb, kanban_db_sync as sync
    from tests.hermes_cli import kanban_sync_support as support
    from contextlib import contextmanager
    home = support.kanban_home(tmp_path, monkeypatch)
    with contextmanager(support.synced_conn)(home) as conn, contextmanager(support.lease_client)() as client:
        sync.set_mode(conn, 'primary')
        task = kb.create_task(conn, title='park', assignee='worker')
        assert kb.claim_task(conn, task)
        run = kb.get_task(conn, task).current_run_id
        uid = sync.run_uid(conn, run)
        helper = kb.create_task(conn, title='helper', assignee='worker')
        kb.link_tasks(conn, helper, task, expected_child_run_id=run)
        assert kb.parent_ids(conn, task) == [helper]
        with kb.write_txn(conn):
            conn.execute('UPDATE kanban_sync_outbox SET created_at=1')
        assert kb.block_task(conn, task, reason='helper', kind='dependency', expected_run_id=run)
        path = conn.execute('PRAGMA database_list').fetchone()[2]
    released = [fields for action, fields in client.calls if action == 'release']
    assert [fields['run_uid'] for fields in released] == [uid]
    assert gate.gate_count(0, [path]) is None
