"""Exact-run CAS and atomic accounting composition for failed results."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_run_finalizer import close_dead_run
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

board = fixtures.board


def _inputs(conn, context):
    fixtures._failed_child(conn, context)
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (context['task_id'],)).fetchone()
    dead = kbd._classify_dead_worker(row['worker_pid'], row['claim_lock'], 1,
                                   task_id=row['id'], run_id=row['current_run_id'], conn=conn)
    return row, dead


def test_close_and_account_roll_back_together(board):
    conn, context = board
    row, dead = _inputs(conn, context)
    with pytest.raises(RuntimeError, match='injected'):
        with kb.write_txn(conn):
            assert close_dead_run(conn, row, dead) is not None
            kbd._record_task_failure(conn, row['id'], dead.error_text, outcome='crashed', allow_nested=True)
            raise RuntimeError('injected after real accounting')
    task = conn.execute('SELECT status, consecutive_failures FROM tasks WHERE id=?', (row['id'],)).fetchone()
    assert tuple(task) == ('running', 0)
    run = conn.execute('SELECT status, ended_at FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    assert tuple(run) == ('running', None)
    assert conn.execute("SELECT count(*) FROM task_events WHERE run_id=? AND kind='crashed'",
                        (context['run_id'],)).fetchone()[0] == 0


def test_close_once_and_account_once(board):
    conn, context = board
    row, dead = _inputs(conn, context)
    for _ in range(2):
        with kb.write_txn(conn):
            if close_dead_run(conn, row, dead) is not None:
                kbd._record_task_failure(conn, row['id'], dead.error_text, outcome='crashed', allow_nested=True)
    assert conn.execute('SELECT consecutive_failures FROM tasks WHERE id=?', (row['id'],)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM task_events WHERE run_id=? AND kind='crashed'",
                        (context['run_id'],)).fetchone()[0] == 1


@pytest.mark.parametrize('changed', ['current_run_id', 'claim_lock', 'worker_started_at', 'worker_pid'])
def test_close_refuses_changed_binding(board, changed):
    conn, context = board
    row, dead = _inputs(conn, context)
    value = -1 if changed in ('current_run_id', 'worker_pid') else 'changed'
    with kb.write_txn(conn):
        conn.execute(f'UPDATE tasks SET {changed}=? WHERE id=?', (value, row['id']))
        assert close_dead_run(conn, row, dead) is None
    assert conn.execute('SELECT status FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()[0] == 'running'


def test_close_requires_transaction(board):
    conn, context = board
    row, dead = _inputs(conn, context)
    with pytest.raises(RuntimeError, match='caller-owned'):
        close_dead_run(conn, row, dead)
