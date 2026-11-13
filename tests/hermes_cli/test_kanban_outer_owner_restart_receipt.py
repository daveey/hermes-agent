"""The managed submitter's own exit 76 reaches the board when no owner can book it. [daveey]

paintbot-tools/t_01a197166756bd53 run 207 (hermes-hub, 2026-11-13 20:06Z): the owner gateway was alive but
wedged, so it never booked the restart (``record_restart_interruption`` runs only in a stopping owner). The
bound interpreter died with exit-1 receipts; the outer ``kanban_worker_client`` saw the owner silent for 283 s
and exited 76 (``OwnerRestartLost``), but printed it only to the shared task log, which a bound run's crash
diagnosis never reads. The sweep booked the run ``crashed`` from the inner receipt and charged the card.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_dispatch_exit_code import KANBAN_OWNER_RESTART_EXIT_CODE
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures
from tools.environments.local import build_subprocess_env

board = fixtures.board
_ROOT = str(Path(__file__).resolve().parents[2])
_SILENT = 'Kanban owner silent for 283s: Gateway connection failed; no local fallback'

# The bound interpreter: binds, books its own failed turn, exits 1 (stdin is the spawn barrier).
_INNER = '''
import json, os, sys
from pathlib import Path
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_connect import connect_closing
from gateway.session_kanban import _record_worker_result
import hermes_cli.kanban_failed_result_observer as observer
observer.arm = lambda context: None
context = json.loads(sys.argv[1])
sys.stdin.read(1)
with connect_closing(Path(context['db'])) as conn, kb.write_txn(conn):
    kb._append_event(conn, context['task_id'], 'worker_bound',
        {'pid': os.getpid(), 'started_at': kbd._process_fingerprint(os.getpid()),
         'claim_lock': context['claim_lock']}, run_id=context['run_id'])
for output in json.loads(sys.argv[2]):
    _record_worker_result(context, 1, output)
raise SystemExit(1)
'''

# The real submitter ``main()``; only its owner connection is replaced by the silent owner's outcome.
_OUTER = '''
import json, os, sys
context = json.loads(sys.argv[1])
sys.stdin.read(1)
os.environ.update(HERMES_KANBAN_BOARD='default', HERMES_KANBAN_TASK=context['task_id'],
                  HERMES_KANBAN_RUN_ID=str(context['run_id']), HERMES_KANBAN_CLAIM_LOCK=context['claim_lock'],
                  HERMES_KANBAN_DB=context['db'])
from hermes_cli import kanban_worker_client as client
async def lost(params, board_db):
    raise client.OwnerRestartLost(sys.argv[2])
client.run = lost
raise SystemExit(client.main())
'''


def _spawn(source, *args):
    return subprocess.Popen([sys.executable, '-c', source, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding='utf-8', env=build_subprocess_env(), cwd=_ROOT)


def _finish(proc):
    out, err = proc.communicate('x', timeout=60)
    return proc.returncode, out, err


def _run_207(conn, context, inner_outputs, *, outer_pid_owner=True):
    """Launch the outer submitter (the task's worker), let its bound interpreter fail, then the outer exit 76."""
    outer = _spawn(_OUTER, json.dumps(context), _SILENT)
    try:
        if outer_pid_owner:
            kbd._set_worker_pid(conn, context['task_id'], outer.pid)
        inner = _spawn(_INNER, json.dumps(context), json.dumps(inner_outputs))
        rc, out, err = _finish(inner)
        assert rc == 1, (out, err)
        rc, out, err = _finish(outer)
        assert rc == KANBAN_OWNER_RESTART_EXIT_CODE, (out, err)
        assert f'Kanban owner restarted: {_SILENT}' in err
        return outer.pid
    finally:
        for proc in (outer,):
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def _results(conn, context):
    return [(row['id'], json.loads(row['payload'])) for row in conn.execute(
        "SELECT id, payload FROM task_events WHERE run_id=? AND kind='worker_result' ORDER BY id",
        (context['run_id'],))]


_TURN = ("⚠️ No reply: Hermes couldn't save this conversation, so it stopped rather than lose your messages.")
_ENVELOPE = 'Worker failed: WorkerPersistenceError: owner_unavailable'


@pytest.mark.parametrize('inner', [[_TURN, _ENVELOPE], [_TURN], ['fixture worker_receipt_timeout']],
                         ids=['run207', 'turn_only', 'card_text'])
@pytest.mark.parametrize('observer', [False, True])
def test_outer_owner_restart_exit_is_booked_and_wins(board, inner, observer):
    conn, context = board
    outer_pid = _run_207(conn, context, inner)
    results = _results(conn, context)
    assert [p['exit_code'] for _, p in results] == [1] * len(inner) + [KANBAN_OWNER_RESTART_EXIT_CODE]
    receipt_id, receipt = results[-1]
    assert receipt['pid'] == outer_pid and receipt['submitter'] is True and receipt['owner_restart'] is True
    assert receipt['result_role'] == 'owner_restart' and receipt['death_proof_version'] == 1
    assert _SILENT in receipt['last_output'] and 'continuable' not in receipt
    kbd.detect_crashed_workers(conn, failed_results_only=observer)
    task = kb.get_task(conn, context['task_id'])
    assert task.status == 'ready' and task.consecutive_failures == 0
    run = conn.execute('SELECT * FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    assert run['outcome'] == 'reclaimed' and 'gateway restart' in run['error']
    (lost,) = conn.execute("SELECT payload FROM task_events WHERE run_id=? AND kind='owner_restart_lost'",
                           (context['run_id'],)).fetchall()
    assert json.loads(lost['payload'])['source_result_event_id'] == receipt_id
    kinds = [r[0] for r in conn.execute('SELECT kind FROM task_events WHERE run_id=?', (context['run_id'],))]
    assert 'crashed' not in kinds and 'failure_finalized' not in kinds


def test_outer_exit_with_no_inner_receipt_is_booked_too(board):
    conn, context = board
    outer = _spawn(_OUTER, json.dumps(context), _SILENT)
    try:
        kbd._set_worker_pid(conn, context['task_id'], outer.pid)
        assert _finish(outer)[0] == KANBAN_OWNER_RESTART_EXIT_CODE
    finally:
        if outer.poll() is None:
            outer.kill()
            outer.wait()
    (_, receipt), = _results(conn, context)
    assert receipt['exit_code'] == KANBAN_OWNER_RESTART_EXIT_CODE and receipt['causal_predecessors'] == []
    kbd.detect_crashed_workers(conn)
    task = kb.get_task(conn, context['task_id'])
    assert task.status == 'ready' and task.consecutive_failures == 0


def test_a_submitter_that_is_not_the_runs_worker_books_nothing(board):
    """A late submitter of an older attempt (the task's worker is someone else) leaves the board alone."""
    conn, context = board
    kbd._set_worker_pid(conn, context['task_id'], os.getpid())
    outer = _spawn(_OUTER, json.dumps(context), _SILENT)
    try:
        assert _finish(outer)[0] == KANBAN_OWNER_RESTART_EXIT_CODE
    finally:
        if outer.poll() is None:
            outer.kill()
            outer.wait()
    assert _results(conn, context) == []


def test_a_moved_claim_books_nothing(board):
    from hermes_cli.kanban_db_connect import connect_closing
    from hermes_cli.kanban_worker_log import record_submitter_owner_restart
    conn, context = board
    kbd._set_worker_pid(conn, context['task_id'], os.getpid())
    with connect_closing(Path(context['db'])) as other, kb.write_txn(other):
        assert not record_submitter_owner_restart(other, dict(context, claim_lock='elsewhere:1'), os.getpid(), 'x')
        assert not record_submitter_owner_restart(other, dict(context, run_id=context['run_id'] + 1),
                                                  os.getpid(), 'x')
    assert _results(conn, context) == []


def test_receipt_store_failure_keeps_the_exit(monkeypatch, tmp_path, capsys):
    from hermes_cli import kanban_worker_client as client
    from hermes_cli import kanban_worker_log
    for key, value in {'HERMES_KANBAN_BOARD': 'default', 'HERMES_KANBAN_TASK': 't1', 'HERMES_KANBAN_RUN_ID': '1',
                       'HERMES_KANBAN_CLAIM_LOCK': 'h:1', 'HERMES_KANBAN_DB': str(tmp_path / 'kanban.db')}.items():
        monkeypatch.setenv(key, value)

    async def lost(params, board_db):
        raise client.OwnerRestartLost(_SILENT)

    def broken(*a, **kw):
        raise OSError('disk gone')
    monkeypatch.setattr(client, 'run', lost)
    monkeypatch.setattr(kanban_worker_log, 'record_submitter_owner_restart', broken)
    assert client.main() == KANBAN_OWNER_RESTART_EXIT_CODE
    err = capsys.readouterr().err
    assert 'receipt could not be recorded: OSError' in err and f'rc={KANBAN_OWNER_RESTART_EXIT_CODE}' in err
