"""Real sync client/SQLite capture against the in-memory Board contract double.

This is not a production Board deployment or network-partition proof.
"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_failed_result_observer as observer
from tests.hermes_cli import test_kanban_failed_result_reconcile as processes
from tests.hermes_cli import test_kanban_sync_run_rows as transport

kanban_home = transport.kanban_home
svc = transport.svc
live = transport.live


@pytest.mark.parametrize('entry', ['observer', 'observer_process', 'dispatcher'])
@pytest.mark.parametrize('rebound', [False, True])
def test_failed_result_closure_survives_real_sync_echo(live, kanban_home, svc, monkeypatch, tmp_path, entry, rebound):
    """``observer_process``: the observer is its own process (kanban_failed_result_service), which never ran the
    board's init, so nothing registered the board as synced there. worker-a1 (2026-11-13, t_01a1d65b80d2e9f4): its
    ``board_mode`` read ``off``, the rate-limited run was closed in the local mirror (``reconciled_by: observer``)
    instead of released to the Board, and no release op was queued."""
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    tid, uid = transport._claimed(live, kanban_home, svc, 'failed receipt transport')
    task = kb.get_task(live, tid)
    assert task is not None
    context = {'db': str(kanban_home / 'kanban.db'), 'task_id': tid,
               'run_id': task.current_run_id, 'claim_lock': task.claim_lock, 'board': transport.BOARD}
    first = processes._failed_child(live, context)
    payload = json.loads(first['payload'])
    assert payload['bound_event_identity']['uid']
    if rebound:
        processes._failed_child(live, context)
        payload = json.loads(live.execute("SELECT payload FROM task_events WHERE kind='worker_result' ORDER BY id DESC LIMIT 1").fetchone()[0])
    from hermes_cli.kanban_failed_result_identity import result_identity_fields
    # Import order is not causal order: retain the original UID but put the old
    # receipt after the later writer sequence in this local mirror.
    with kb.write_txn(live):
        kb._append_event(live, tid, 'worker_result',
                         {**payload, **result_identity_fields(live, context), 'last_output': 'causal transport failure'},
                         run_id=task.current_run_id)
        live.execute('UPDATE task_events SET id=? WHERE id=?', (first['id'] + 1000, first['id']))
    transport._round(kanban_home, svc)
    if entry.startswith('observer'):
        with monkeypatch.context() as m:
            if entry == 'observer_process':
                m.setattr(sync, '_SYNCED_PATHS', set())
            observer.observe({**context, 'ready_path': str(tmp_path / 'ready'),
                              'observer_unit': 'fixture.service'})
    else:
        dispatch.detect_crashed_workers(live)
    # A Board-primary failure is released to the Board, never closed in the
    # local mirror first (a competing local close is what charged one run twice).
    from hermes_cli.kanban_failure_release import queued
    assert live.execute('SELECT status FROM task_runs WHERE uid=?', (uid,)).fetchone()[0] == 'running'
    assert queued(live, task.current_run_id)
    for _ in range(3):
        transport._round(kanban_home, svc)
        dispatch.detect_crashed_workers(live)
    transport._assert_ended(live, svc, uid, 'crashed')
    local = kb.get_task(live, tid)
    assert local is not None
    assert local.consecutive_failures == 1
    rows = svc.board(transport.BOARD)['rows']
    assert rows['tasks'][tid]['consecutive_failures'] == 1
    crashes = [row for row in rows['task_events'].values() if row.get('task_id') == tid and row.get('kind') == 'crashed']
    assert len(crashes) == 1
    evidence = json.loads(crashes[0]['payload'])
    assert evidence['worker_output'] == 'causal transport failure'
    assert evidence['source_result_event_uid'] in rows['task_events']
    assert evidence['result_recorded_at'] <= evidence['reconciled_at']
    assert tid not in svc.board(transport.BOARD)['leases']
