"""Probe writers preserve exact auxiliary state across custody races."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json

import pytest

from hermes_cli import kanban_ask_resolved as ask, kanban_ask_resolved_backfill as backfill
from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_home_transfer as transfer, kanban_wake_check as wake
from tests.hermes_cli import test_home_probe_custody as support

board = support.board


def snapshot(conn, tid):
    """Include every task-keyed local table, not just synced task/event rows."""
    result = {}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        key = 'id' if table == 'tasks' else 'task_id'
        if key in columns:
            result[table] = sorted((tuple(r) for r in conn.execute(
                f'SELECT * FROM "{table}" WHERE "{key}"=?', (tid,))), key=repr)
    return result


def freeze(conn, tid, record, phase):
    with dbc.write_txn(conn):
        transfer.prepare(conn, tid, source=record['source'], destination='hub',
                         nonce=record['nonce'], expected_version=7)
        if phase == 'pending':
            transfer.finalize_source(conn, record)
        elif phase == 'malformed':
            conn.execute('DROP TRIGGER IF EXISTS home_prepared1_marker_update')
            conn.execute("UPDATE kanban_home_prepared1 SET source='' WHERE task_id=?", (tid,))
        elif phase == 'aborted':
            transfer.abort_source(conn, {**record, 'phase': 'aborted', 'rows': []})


@pytest.mark.parametrize('is_ask', [False, True])
@pytest.mark.parametrize('phase', ['prepared', 'pending', 'malformed', 'aborted'])
@pytest.mark.parametrize('rc', [0, 1, 2])
def test_late_results_preserve_all_local_tables_and_healthy_neighbor(board, is_ask, phase, rc):
    tid, record, _ = support.source_park(board, ask=is_ask)
    healthy, _, _ = support.source_park(board, ask=is_ask)
    apply = ask.apply_result if is_ask else wake.apply_result
    state = wake.get_state(board, tid)
    other = wake.get_state(board, healthy)
    # Real bookkeeping seeds wake episode, barrier and confirmation tables before capture.
    apply(board, tid, state['cmd'], 1, 'not yet', generation=state['generation'], now=999)
    from hermes_cli.kanban_home_probe import claim_wake
    assert claim_wake(board, tid, state['cmd'], state['every'], state['generation'],
                      state.get('purpose'), 'blocked' if is_ask else 'scheduled', 1120, 'inflight')
    state = wake.get_state(board, tid)
    with dbc.write_txn(board):
        board.execute('DELETE FROM kanban_sync_outbox')
    freeze(board, tid, record, phase)
    before = snapshot(board, tid)
    apply(board, tid, state['cmd'], rc, 'not yet', generation=state['generation'], now=1121)
    if phase != 'aborted':
        assert transfer.frozen(board, tid)
        assert snapshot(board, tid) == before
    else:
        assert not transfer.frozen(board, tid)
        assert wake.get_state(board, tid) != state
    apply(board, healthy, other['cmd'], 1, 'not yet', generation=other['generation'], now=1001)
    assert wake.get_state(board, healthy)['last_at'] == 1001


@pytest.mark.parametrize('action', ['restore', 'lost', 'silent', 'backfill'])
def test_reconcile_writer_entry_race_keeps_local_tables(board, monkeypatch, action):
    tid, record, _ = support.source_park(board, ask=True)
    with dbc.write_txn(board):
        if action == 'silent':
            board.execute("UPDATE task_events SET payload=json_set(payload,'$.probe_host','hub') "
                          "WHERE task_id=? AND kind='blocked'", (tid,))
        if action in ('lost', 'backfill'):
            kind = ask.RESTORED_EVENT if action == 'lost' else ask.BROKEN_EVENT
            kb._append_event(board, tid, kind, {})
        # Retain source probe through prepare preconditions, delete at writer seam below.
        board.execute('DELETE FROM kanban_sync_outbox')
    original = kb.write_txn
    entered = []
    expected = {}

    @contextmanager
    def writer(c, *args, **kwargs):
        if not entered:
            entered.append(True)
            freeze(c, tid, record, 'pending')
            expected.update(snapshot(c, tid))
        with original(c, *args, **kwargs):
            yield

    # Reconcile needs a missing row to select the restoration/silent/backfill writers.
    # An explicitly foreign probe permits preparation without local execution state.
    with dbc.write_txn(board):
        board.execute("UPDATE task_events SET payload=json_set(payload,'$.probe_host','hub') "
                      "WHERE task_id=? AND kind='blocked'", (tid,))
        board.execute('DELETE FROM task_wake_checks WHERE task_id=?', (tid,))
        board.execute('DELETE FROM kanban_sync_outbox')
        # Refresh the exact manifest after the genuine filing edit.
        for entry in record['rows']:
            if entry['table'] == 'task_events' and entry['fields'].get('kind') == 'blocked':
                payload = json.loads(entry['fields']['payload'])
                payload['probe_host'] = 'hub'
                entry['fields']['payload'] = json.dumps(payload)
    # For local restoration/loss paths, own gateway is hub; source identity remains worker-a3.
    monkeypatch.setattr(wake, 'own_gateway', lambda c: 'worker-a3' if action == 'silent' else 'hub')
    monkeypatch.setattr(wake, 'foreign_ids', lambda c, own: set())
    monkeypatch.setattr(kb, 'write_txn', writer)
    if action == 'backfill':
        backfill.backfill(board)
    else:
        ask.reconcile(board, now=2000000000)
    assert entered and transfer.frozen(board, tid)
    after = snapshot(board, tid)
    assert after == expected


@pytest.mark.parametrize('known', [True, False])
def test_backfill_respects_completed_historical_host_and_unknown_custody(board, known):
    support.support.prepare_destination(board)
    record = support.probe_manifest(ask=True)
    event = copy.deepcopy(record['rows'][2])
    event['uid'] = event['fields']['uid'] = 'legacy-drop'
    event['fields'].update(kind=ask.BROKEN_EVENT, ord=2, created_at=1001, payload='{}')
    record['rows'].append(event)
    support.support.materialize(board, record)
    support.support.complete_feed(board, record)
    tid = record['task']
    if not known:
        with dbc.write_txn(board):
            board.execute('DELETE FROM kanban_home_materialized1')
            board.execute('UPDATE task_events SET origin_gateway=NULL WHERE task_id=?', (tid,))
    before = snapshot(board, tid)
    assert backfill.backfill(board) == []
    assert snapshot(board, tid) == before


def test_late_probe_followup_cannot_record_decision_recovery(board):
    from hermes_cli import kanban_decision_ask as decision
    tid, record, _ = support.source_park(board)
    with dbc.write_txn(board):
        kb._append_event(board, tid, decision.NOTICE_KIND, {'why': 'question'})
        board.execute('DELETE FROM kanban_sync_outbox')
    freeze(board, tid, record, 'pending')
    before = snapshot(board, tid)
    assert not decision.escalate(board, tid, 'late unanswered probe', trust_probe=True)
    assert snapshot(board, tid) == before
