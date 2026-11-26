"""Real Board sync/pickup carrier; service fake stamps sender like BoardTx.put."""
import json
import time

import pytest

from hermes_cli import kanban_capacity_evidence as cap
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_dispatch_skip as skips
from hermes_cli import kanban_nonrunning_transport as transport
from hermes_cli import kanban_execution_source as source
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as helpers

svc = helpers.svc
gws = helpers.gws


def prepared(gws):
    home, worker = gws
    home.enter()
    tid = kb.create_task(home.conn, title='Portable refusal', assignee='default', runs_on=['linux'])
    home.round()
    worker.round()
    worker.enter()
    assert tid in wn.pickup(worker.conn, budget=10)
    assert kb.claim_task(worker.conn, tid)
    task = kb.get_task(worker.conn, tid)
    import os
    with kb.write_txn(worker.conn):
        kb._append_event(worker.conn, tid, 'owner_admitted', {'session_id': 'isolated-source'}, run_id=task.current_run_id)
        kb._append_event(worker.conn, tid, 'worker_bound', {'pid': os.getpid(), 'claim_lock': task.claim_lock}, run_id=task.current_run_id)
    worker.round()  # publish the grant before the real Board-primary release
    dispatch._record_task_failure(worker.conn, tid, '', outcome='released', release_claim=True, end_run=True, infrastructure=True)
    worker.round()
    home.round()
    worker.round()
    worker.enter()
    wn.pickup(worker.conn, budget=10)
    assert kb.get_task(worker.conn, tid).status == 'ready'
    return home, worker, tid


def publish(worker, tid):
    worker.enter()
    captured = cap.capture(worker.conn)
    result = dispatch.DispatchResult()
    assert kb.claim_task(worker.conn, tid) is None
    reason = sync.pop_claim_deferral(tid)
    assert reason == 'usage_accounting_pending'
    result.claim_deferred.append((tid, reason))
    assert skips.record_tick(worker.conn, result, captured=captured, machine=worker.name) == 1
    worker.round()
    return captured['observed_at']


def test_worker_refusal_reaches_home_with_transport_origin(gws):
    home, worker, tid = prepared(gws)
    at = publish(worker, tid)
    home.round()
    home.enter()
    why = transport.read(home.conn, [tid], board='default', now=at + 1)[tid]
    assert why['code'] == 'usage_accounting_pending'
    assert why['freshness'] == 'current'
    assert why['observer'] == worker.name
    from hermes_cli import kanban_execution_source as source
    with source.transaction(home.conn):
        sources = source.assemble(home.conn, [tid], board='default', gateway=home.name, profile='default', observed_at=at + 1)
    raw = source.raw({'id': tid}, sources[tid])
    assert raw['non_running_reason']['code'] == why['code']
    assert raw['non_running_reason']['freshness'] == 'current'
    # No machine-local snapshot exists at the home.
    assert not home.conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_capacity_observation'").fetchone()
    assert transport.read(home.conn, [tid], board='default', now=at + 180)[tid]['freshness'] == 'unknown'


@pytest.mark.parametrize('change', ['policy', 'same_status', 'forged_observer', 'missing_origin'])
def test_invalid_foreign_carrier_is_unknown(gws, change):
    home, worker, tid = prepared(gws)
    at = publish(worker, tid)
    home.round()
    home.enter()
    if change == 'policy':
        from hermes_cli.kanban_db_edit import edit_task
        assert edit_task(home.conn, tid, title='Changed evaluated policy')
    elif change == 'same_status':
        with kb.write_txn(home.conn):
            kb._append_event(home.conn, tid, 'reopened', {'status': 'ready'})
    else:
        # Disposable-store corruption controls, not production writes.
        event = home.conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='dispatch_skipped'", (tid,)).fetchone()
        with kb.write_txn(home.conn):
            if change == 'missing_origin':
                home.conn.execute('UPDATE task_events SET origin_gateway=NULL WHERE id=?', (event['id'],))
            else:
                payload = json.loads(event['payload'])
                payload['non_running_v1']['observer'] = 'forged'
                home.conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), event['id']))
    assert transport.read(home.conn, [tid], board='default', now=at + 1)[tid]['freshness'] == 'unknown'


@pytest.mark.parametrize('change', ['unknown', 'expired', 'out_of_order'])
def test_newer_unknown_or_expired_cannot_resurface_old_refusal(gws, change):
    home, worker, tid = prepared(gws)
    at = publish(worker, tid)
    worker.enter()
    event = worker.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='dispatch_skipped' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    payload = json.loads(event['payload'])
    payload['non_running_v1']['observed_at'] = at - 1 if change == 'out_of_order' else at + 1
    if change == 'expired':
        payload['non_running_v1']['valid_until'] = at + 2
    else:
        payload['reason'], payload['detail'] = 'native_restriction', 'unknown-policy'
    with kb.write_txn(worker.conn):
        kb._append_event(worker.conn, tid, 'dispatch_skipped', payload)
    worker.round()
    home.round()
    home.enter()
    why = transport.read(home.conn, [tid], board='default', now=at + 3)[tid]
    assert why['freshness'] == ('current' if change == 'out_of_order' else 'unknown')
    if change == 'out_of_order':
        assert why['code'] == 'usage_accounting_pending'


def test_corrupt_carrier_isolated_to_its_task(gws):
    home, worker, broken = prepared(gws)
    publish(worker, broken)
    home, worker, good = prepared(gws)
    at = publish(worker, good)
    home.round()
    home.enter()
    with kb.write_txn(home.conn):
        home.conn.execute("UPDATE task_events SET payload='not-json' WHERE task_id=? AND kind='dispatch_skipped'", (broken,))
    reasons = transport.read(home.conn, [broken, good], board='default', now=at + 1)
    assert reasons[broken]['freshness'] == 'unknown'
    assert reasons[good]['freshness'] == 'current'
    assert reasons[good]['code'] == 'usage_accounting_pending'


def test_history_cut_cannot_hide_newer_unknown_or_scan_all_events(gws):
    home, worker, tid = prepared(gws)
    at = publish(worker, tid)
    worker.enter()
    payload = json.loads(worker.conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='dispatch_skipped' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])
    unknown = json.loads(json.dumps(payload))
    unknown['non_running_v1']['observed_at'] = at + 1
    unknown['reason'], unknown['detail'] = 'native_restriction', 'unknown-policy'
    with kb.write_txn(worker.conn):
        kb._append_event(worker.conn, tid, 'dispatch_skipped', unknown)
        for _ in range(17):
            kb._append_event(worker.conn, tid, 'dispatch_skipped', payload)
    worker.round()
    home.round()
    home.enter()
    assert transport.read(home.conn, [tid], board='default', now=at + 2)[tid]['freshness'] == 'unknown'
    def measure():
        steps = []
        home.conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
        try:
            assert transport.read(home.conn, [tid], board='default', now=at + 2)[tid]['freshness'] == 'unknown'
        finally:
            home.conn.set_progress_handler(None, 0)
        return len(steps)
    baseline = measure()
    with kb.write_txn(home.conn):
        for _ in range(3000):
            kb._append_event(home.conn, tid, 'dispatch_skipped', payload)
    large = measure()
    assert large <= baseline + 20, (baseline, large)


def test_replica_reason_acquisition_query_budget_and_memo_immutability(gws):
    from copy import deepcopy
    home, worker, tid = prepared(gws)
    at = publish(worker, tid)
    home.round()
    home.enter()
    ids = [tid] + [kb.create_task(home.conn, title='Budget control', assignee='default') for _ in range(40)]
    rows = [dict(home.conn.execute('SELECT * FROM tasks WHERE id=?', (key,)).fetchone()) for key in ids]
    before = deepcopy(rows)
    counts = []
    for selected in (rows[:1], rows):
        sql = []
        home.conn.set_trace_callback(sql.append)
        try:
            with source.transaction(home.conn):
                result = transport.replica_rows(home.conn, selected, now=at + 1)
        finally:
            home.conn.set_trace_callback(None)
        counts.append(len(sql))
        assert result[0]['_non_running_reason']['freshness'] == 'current'
    assert counts[1] <= counts[0] + 1, counts  # one chunk, not one inventory per task
    assert rows == before
