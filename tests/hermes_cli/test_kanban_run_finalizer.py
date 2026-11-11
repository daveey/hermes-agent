"""Exact-run CAS and atomic accounting composition for failed results."""
import json

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


def test_failed_result_sweep_avoids_owner_store_and_preserves_times(board, monkeypatch):
    from hermes_cli import kanban_owner_recovery as owner
    conn, context = board
    result = fixtures._failed_child(conn, context)
    def forbidden(*args, **kwargs):
        pytest.fail('failed-result reconciliation must not consult owner state.db')
    monkeypatch.setattr(owner, 'owner_reclaim_blocked', forbidden)
    monkeypatch.setattr(owner, 'apply_attempt_note', forbidden)
    sweep = kbd._reclaim_dead_workers(conn, failed_results_only=True)
    assert sweep.crashed == [context['task_id']]
    row = conn.execute('SELECT metadata FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    metadata = json.loads(row[0])
    assert metadata['source_result_event_id'] == result['id']
    assert metadata['result_recorded_at'] == result['created_at']
    assert metadata['reconciled_at'] >= result['created_at']
    assert metadata['reconciled_by'] == 'observer'
    assert kbd._reclaim_dead_workers(conn, failed_results_only=True).crashed == []
    assert conn.execute('SELECT consecutive_failures FROM tasks WHERE id=?',
                        (context['task_id'],)).fetchone()[0] == 1


def test_sweep_accounting_exception_rolls_back_real_closure(board, monkeypatch):
    conn, context = board
    fixtures._failed_child(conn, context)
    real_account = kbd._account_crashes
    def fail_after_account(*args, **kwargs):
        real_account(*args, **kwargs)
        raise RuntimeError('after accounting')
    monkeypatch.setattr(kbd, '_account_crashes', fail_after_account)
    with pytest.raises(RuntimeError, match='after accounting'):
        kbd._reclaim_dead_workers(conn, failed_results_only=True)
    assert conn.execute('SELECT status, consecutive_failures FROM tasks WHERE id=?',
                        (context['task_id'],)).fetchone()[:] == ('running', 0)
    assert conn.execute('SELECT status, ended_at FROM task_runs WHERE id=?',
                        (context['run_id'],)).fetchone()[:] == ('running', None)


def test_systemic_batch_preserves_one_fix_owner_inside_transaction(board):
    conn, context = board
    contexts = [context]
    for index in range(2):
        tid = kb.create_task(conn, title=f'additional failed result {index}', assignee='default')
        task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
        assert task is not None
        contexts.append({**context, 'task_id': tid, 'run_id': task.current_run_id,
                         'claim_lock': task.claim_lock})
    for item in contexts:
        fixtures._failed_child(conn, item)
    sweep = kbd._reclaim_dead_workers(conn, failed_results_only=True)
    assert set(sweep.auto_blocked) == {item['task_id'] for item in contexts}
    fixes = conn.execute("SELECT id FROM tasks WHERE idempotency_key LIKE 'systemic-crash:%'").fetchall()
    assert len(fixes) == 1
    children = conn.execute('SELECT child_id FROM task_links WHERE parent_id=?', (fixes[0][0],)).fetchall()
    assert {row[0] for row in children} == {item['task_id'] for item in contexts}
    assert kbd._reclaim_dead_workers(conn, failed_results_only=True).crashed == []
    for item in contexts:
        assert conn.execute('SELECT consecutive_failures FROM tasks WHERE id=?',
                            (item['task_id'],)).fetchone()[0] == 1
