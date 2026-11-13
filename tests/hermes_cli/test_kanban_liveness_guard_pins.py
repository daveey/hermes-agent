"""Pins for three guards of the exited-worker liveness fix that its own tests never reached. [daveey]

Post-merge mutation check of PR #2420 (t_01a1d61dee8aa5aa): each case below fails when its guard is dropped.
- a clean (rc=0) modern exit books its death-proof result but never arms the failed-result observer;
- a final result followed by a newer binding (a live next incarnation) is not an exited worker;
- with no result booked, a local claim whose every recorded process is dead is an exited worker.
"""
import json
import os
import subprocess
import sys
import threading

from gateway import session_kanban as sk
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_failed_result_observer as observer
from hermes_cli.kanban_comment_liveness import EXITED_NOTE, worker_exited
from tests.hermes_cli import test_kanban_owner_restart_failed_receipt as shapes
from tools import kanban_tools as kt
from tools.environments.local import build_subprocess_env

board = shapes.board


def _bind(conn, context, pid):
    with kb.write_txn(conn):
        kb._append_event(conn, context['task_id'], 'worker_bound',
                         {'pid': pid, 'started_at': dispatch._process_fingerprint(pid),
                          'claim_lock': context['claim_lock']}, run_id=context['run_id'])


def _comment(context, monkeypatch):
    monkeypatch.setattr(kt, '_persisted_identity', lambda: 'default')
    return json.loads(kt._handle_comment({'task_id': context['task_id'], 'body': 'are you there'}))


def test_the_failed_result_observer_is_armed_only_for_a_nonzero_modern_exit(board, monkeypatch):
    conn, context = board
    shapes._failed_child(conn, context)
    shapes.book(context, True)             # the run's evidence is modern: a clean exit joins it
    _bind(conn, context, os.getpid())      # this interpreter is the run's bound worker
    armed = []
    monkeypatch.setattr(observer, 'arm', armed.append)
    monkeypatch.setattr(sk, '_TURNS_DONE', threading.Event())
    sk._record_worker_result(context, 0, 'finished cleanly')
    assert json.loads(shapes.latest(conn)['payload'])['death_proof_version'] == 1  # strict proof held
    assert armed == []
    sk._record_worker_result(context, 1, 'failed')
    assert [c['run_id'] for c in armed] == [context['run_id']]


def test_a_final_result_before_a_newer_live_binding_is_not_an_exited_worker(board, monkeypatch):
    conn, context = board
    shapes._failed_child(conn, context)    # a final (non-continuable) exit-1 result; that child is dead
    assert worker_exited(conn, context['task_id'], context['run_id'])
    _bind(conn, context, os.getpid())      # a newer incarnation of the same run binds and is alive
    assert not worker_exited(conn, context['task_id'], context['run_id'])
    out = _comment(context, monkeypatch)
    assert out['live_run'] is True and 'worker_exited' not in out


def test_every_recorded_process_dead_without_a_result_is_an_exited_worker(board, monkeypatch):
    conn, context = board
    outer = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                             stdin=subprocess.PIPE, env=build_subprocess_env())
    inner = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.read()'],
                             stdin=subprocess.PIPE, env=build_subprocess_env())
    try:
        dispatch._set_worker_pid(conn, context['task_id'], outer.pid)
        _bind(conn, context, inner.pid)
        assert not worker_exited(conn, context['task_id'], context['run_id'])  # both alive
        inner.communicate(timeout=10)
        assert not worker_exited(conn, context['task_id'], context['run_id'])  # the outer still lives
    finally:
        outer.communicate(timeout=10)
        if inner.poll() is None:
            inner.communicate(timeout=10)
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='worker_result'").fetchone()[0] == 0
    assert kb._claim_is_local(context['claim_lock'], kb._host_prefix())
    assert worker_exited(conn, context['task_id'], context['run_id'])
    out = _comment(context, monkeypatch)
    assert out['live_run'] is False and out['worker_exited'] is True and out['delivery'] == EXITED_NOTE
