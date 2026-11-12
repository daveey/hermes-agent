"""Expiry signals must remain fenced to the selected attempt and exact incarnation."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_owner_recovery as owner
from tests.hermes_cli.test_kanban_failed_result_runtime import _enforce, _expire
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures

board = fixtures.board


@pytest.fixture
def expired(board, monkeypatch):
    conn, context = board
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET worker_pid=301, worker_started_at=? WHERE id=?',
                     ('epoch|301', context['task_id']))
        kb._append_event(conn, context['task_id'], 'worker_bound',
                         {'pid': 302, 'started_at': 'epoch|302', 'claim_lock': context['claim_lock']},
                         run_id=context['run_id'])
        kb._append_event(conn, context['task_id'], 'worker_result',
                         {'death_proof_version': 1}, run_id=context['run_id'])
    monkeypatch.setattr(kb, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(dispatch, '_process_fingerprint', lambda pid: f'epoch|{pid}')
    monkeypatch.setattr('hermes_cli.kanban_failed_result.process_gone', lambda *args: False)
    monkeypatch.setattr(dispatch, '_poll_worker_exit', lambda *args: True)
    return conn, context


def _replace(conn, context):
    with kb.write_txn(conn):
        lock = context['claim_lock'] + ':replacement'
        run = conn.execute("INSERT INTO task_runs(task_id,status,started_at,claim_lock) "
                           "VALUES (?, 'running', 4000000000, ?)", (context['task_id'], lock)).lastrowid
        conn.execute('UPDATE tasks SET current_run_id=?, claim_lock=?, worker_pid=401, '
                     'worker_started_at=?, claim_expires=4000000000 WHERE id=?',
                     (run, lock, 'epoch|401', context['task_id']))
        kb._append_event(conn, context['task_id'], 'worker_bound',
                         {'pid': 402, 'started_at': 'epoch|402', 'claim_lock': lock}, run_id=run)
        kb._append_event(conn, context['task_id'], 'worker_result',
                         {'death_proof_version': 1}, run_id=run)


@pytest.mark.parametrize('entry', ['ttl', 'max_runtime'])
@pytest.mark.parametrize('boundary', ['gate', 'grace', 'escalation'])
def test_expiry_never_redirects_signals_to_replacement(expired, monkeypatch, entry, boundary):
    conn, context = expired
    _expire(conn, context, entry)
    signals = []
    gate = owner.owner_reclaim_blocked
    replaced = False

    def gated(*args, **kwargs):
        nonlocal replaced
        result = gate(*args, **kwargs)
        if boundary == 'gate' and not replaced:
            _replace(conn, context)
            replaced = True
        return result

    def poll(*args):
        nonlocal replaced
        if boundary != 'gate' and not replaced:
            _replace(conn, context)
            replaced = True
        return boundary != 'escalation'

    monkeypatch.setattr(owner, 'owner_reclaim_blocked', gated)
    monkeypatch.setattr(dispatch, '_poll_worker_exit', poll)
    _enforce(conn, entry, signal_fn=lambda pid, sig: signals.append((pid, sig)))
    assert replaced
    assert not any(pid in (401, 402) for pid, _ in signals), signals
    if boundary == 'gate':
        assert signals == []
    else:
        assert len(signals) == 1, signals
    assert conn.execute('SELECT current_run_id FROM tasks WHERE id=?',
                        (context['task_id'],)).fetchone()[0] != context['run_id']
    assert conn.execute('SELECT consecutive_failures FROM tasks WHERE id=?',
                        (context['task_id'],)).fetchone()[0] == 0


@pytest.mark.parametrize('entry', ['ttl', 'max_runtime'])
@pytest.mark.parametrize('target', [301, 302], ids=['outer', 'bound'])
@pytest.mark.parametrize('boundary', ['sigterm', 'sigkill'])
def test_expiry_rechecks_exact_birth_at_each_signal(expired, monkeypatch, entry, target, boundary):
    conn, context = expired
    _expire(conn, context, entry)
    births = {301: 'epoch|301', 302: 'epoch|302'}
    signals = []
    terminate = dispatch._terminate_reclaimed_worker

    def terminating(pid, *args, **kwargs):
        if pid == target and boundary == 'sigterm':
            births[pid] = f'epoch|{pid + 1}'
        return terminate(pid, *args, **kwargs)

    def poll(pid, *args):
        if pid == target and boundary == 'sigkill':
            births[pid] = f'epoch|{pid + 1}'
            return False
        return True

    monkeypatch.setattr(dispatch, '_process_fingerprint', births.get)
    monkeypatch.setattr(dispatch, '_terminate_reclaimed_worker', terminating)
    monkeypatch.setattr(dispatch, '_poll_worker_exit', poll)
    _enforce(conn, entry, signal_fn=lambda pid, sig: signals.append((pid, sig)))
    from signal import SIGTERM
    targeted = [sig for pid, sig in signals if pid == target]
    assert targeted == ([] if boundary == 'sigterm' else [SIGTERM]), signals
    assert conn.execute('SELECT status, ended_at FROM task_runs WHERE id=?',
                        (context['run_id'],)).fetchone()[:] == ('running', None)
