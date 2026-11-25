"""Independent exact-head review custody regressions; expected invariants."""
import copy
import json
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_wake_check as wake, kanban_ask_resolved as ask
from hermes_cli import kanban_home_transfer as transfer
from tests.hermes_cli import test_home_probe_custody as support

board = support.board

@pytest.mark.parametrize('is_ask', [False, True])
def test_pending_drops_inflight_result_without_probe_mutation(board, is_ask):
    tid, record, _ = support.source_park(board, ask=is_ask)
    state = wake.get_state(board, tid)
    if not is_ask:
        wake.apply_result(board, tid, state['cmd'], 1, 'not yet', generation=state['generation'], now=999)
        with dbc.write_txn(board):
            board.execute('DELETE FROM kanban_sync_outbox')
    with dbc.write_txn(board):
        transfer.prepare(board, tid, source=record['source'], destination='hub', nonce=record['nonce'], expected_version=7)
        transfer.finalize_source(board, record)
    assert transfer.frozen(board, tid)
    before = dict(wake.get_state(board, tid))
    apply = ask.apply_result if is_ask else wake.apply_result
    apply(board, tid, state['cmd'], 1, 'not yet', generation=state['generation'], now=1001)
    assert dict(wake.get_state(board, tid)) == before


def test_backfill_skips_pending_ask_and_restores_neighbor(board):
    support.support.prepare_destination(board)
    record = support.probe_manifest(ask=True)
    event = copy.deepcopy(record['rows'][2])
    event['uid'] = event['fields']['uid'] = 'drop-event'
    event['fields'].update(kind=ask.BROKEN_EVENT, ord=2, created_at=1001, payload='{}')
    record['rows'].append(event)
    support.support.materialize(board, record)
    healthy = kb.create_task(board, title='healthy')
    with dbc.write_txn(board):
        board.execute("UPDATE tasks SET status='blocked',human_request=?,created_on='hub' WHERE id=?", (json.dumps({'resolved_check':'true'}),healthy))
        kb._append_event(board, healthy, 'blocked', {})
    assert transfer.frozen(board, record['task'])
    result = ask.reconcile(board, now=1002)
    assert result['restored'] == [healthy]


def test_reconcile_rechecks_custody_after_selection(board, monkeypatch):
    from contextlib import contextmanager
    tid, record, _ = support.source_park(board, ask=True)
    with dbc.write_txn(board):
        board.execute("UPDATE task_wake_checks SET cmd='old command' WHERE task_id=?", (tid,))
        board.execute("UPDATE task_events SET payload=json_set(payload,'$.probe_host','hub') WHERE task_id=? AND kind='blocked'", (tid,))
        board.execute('DELETE FROM kanban_sync_outbox')
        for entry in record['rows']:
            if entry['table'] == 'task_events' and entry['fields'].get('kind') == 'blocked':
                payload = json.loads(entry['fields']['payload'])
                payload['probe_host'] = 'hub'
                entry['fields']['payload'] = json.dumps(payload)
    before = dict(wake.get_state(board, tid))
    original = kb.write_txn
    admitted = False
    @contextmanager
    def interleaved(conn, *args, **kwargs):
        nonlocal admitted
        if not admitted:
            admitted = True
            with dbc.write_txn(conn):
                transfer.prepare(conn, tid, source=record['source'], destination='hub', nonce=record['nonce'], expected_version=7)
                transfer.finalize_source(conn, record)
        with original(conn, *args, **kwargs):
            yield
    monkeypatch.setattr(kb, 'write_txn', interleaved)
    ask.reconcile(board, now=1001)
    assert admitted and transfer.frozen(board, tid)
    assert dict(wake.get_state(board, tid)) == before
