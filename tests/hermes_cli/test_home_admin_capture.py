"""Administrative result custody never refreshes a replay from today's home. [daveey]"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_connect as dbc, kanban_sync_client as client
from hermes_cli import kanban_operator_labels as labels, kanban_home_memo as memo
from hermes_cli import kanban_home_transfer as source
from tests.hermes_cli import test_kanban_home_transfer_fence as support

board = support.board


def hub_fixture(board, monkeypatch, epoch='original-epoch-00001', tries=0):
    task = support.seed(board)
    event = 'event-result-0000001'
    with dbc.write_txn(board):
        board.execute('CREATE TABLE operator_label_jobs(event_uid TEXT PRIMARY KEY,delivery_attempts INTEGER)')
        board.execute('INSERT INTO operator_label_jobs VALUES (?,?)', (event, tries))
    sent = []
    def send(_board, ops):
        sent.extend(ops)
        return {'results':[{'status':'applied', 'client_op_id':ops[0]['client_op_id']}]}
    def evidence(_board, table, _uid):
        if table == 'task_events':
            return {'state':'live', 'row':{**sent[-1]['fields'], 'uid':event, 'origin_gateway':'worker-a3'}}
        return {'state':'live', 'row':{'id':task, 'home_generation':epoch}}
    hub = SimpleNamespace(ops=send, close=lambda: None, row_evidence=evidence)
    monkeypatch.setattr(client, 'Hub', lambda *a: hub)
    monkeypatch.setattr(client, 'load_identity', lambda: None)
    return task, event, sent, hub


@pytest.mark.parametrize('tries', [0, 1])
def test_label_first_reservation_and_legacy_unknown_are_stable_on_retry(board, monkeypatch, tries):
    task, event, sent, hub = hub_fixture(board, monkeypatch, tries=tries)
    labels._push_event(board, 'default', 'worker-a3', task, event, {'outcome':'verified'})
    expected = {task:'original-epoch-00001'} if tries == 0 else None
    assert sent[0].get('home_generations') == expected
    previous = hub.row_evidence
    hub.row_evidence = lambda b,t,u: pytest.fail('retry fetched a newer epoch') if t == 'tasks' else previous(b,t,u)
    labels._push_event(board, 'default', 'worker-a3', task, event, {'outcome':'verified'})
    assert sent[1] == sent[0]
    with pytest.raises(ValueError, match='identity changed'):
        labels._push_event(board, 'default', 'worker-a3', task, event, {'outcome':'changed'})
    stored = board.execute('SELECT request_json FROM kanban_home_delivery1').fetchone()[0]
    assert 'home_generations' not in json.loads(stored)


def test_unknown_admin_result_prevents_source_prepare_until_settled(board, monkeypatch):
    task, event, _sent, hub = hub_fixture(board, monkeypatch)
    hub.ops = lambda *a: (_ for _ in ()).throw(TimeoutError('lost result'))
    with pytest.raises(TimeoutError):
        labels._push_event(board, 'default', 'worker-a3', task, event, {'outcome':'verified'})
    with dbc.write_txn(board):
        board.execute('DELETE FROM kanban_sync_outbox')
    with dbc.write_txn(board), pytest.raises(ValueError, match='drained administrative intent'):
        source.prepare(board, task, source='worker-a3', destination='hub', nonce='next-attempt-00001', expected_version=7)
    with dbc.write_txn(board):
        memo.settled(board, event, 'applied')
        source.prepare(board, task, source='worker-a3', destination='hub', nonce='next-attempt-00001', expected_version=7)
    assert source.frozen(board, task)


@pytest.mark.parametrize('fault', ['wrong_receipt', 'wrong_readback'])
def test_label_result_never_settles_unconfirmed_delivery(board, monkeypatch, fault):
    task, event, _sent, hub = hub_fixture(board, monkeypatch)
    original = hub.ops
    if fault == 'wrong_receipt':
        def bad(*args):
            result = original(*args)
            result['results'][0]['client_op_id'] = 'unrelated'
            return result
        hub.ops = bad
    else:
        original_proof = hub.row_evidence
        hub.row_evidence = lambda b,t,u: {'state':'missing'} if t == 'task_events' else original_proof(b,t,u)
    with pytest.raises(ValueError, match='unconfirmed'):
        labels._push_event(board, 'default', 'worker-a3', task, event, {'outcome':'verified'})
    assert memo.pending(board, task)


@pytest.mark.parametrize('reference', ['task', 'actor_task'])
def test_pending_containment_reference_prevents_source_prepare(board, reference):
    from hermes_cli import kanban_containment_canonical as canonical, kanban_db_sync as sync
    task = support.seed(board)
    op = {'op':'contain', 'task':'t_1234567890abcdef', 'actor_task':'t_1234567890abcdef', 'event_uid':sync.uuid7(),
          'client_op_id':sync.uuid7(), 'actor_run_uid':sync.uuid7(), 'payload':{}, reference:task}
    canonical.stage(board, board='default', gateway='worker-a3', op=op)
    with dbc.write_txn(board):
        board.execute('DELETE FROM kanban_sync_outbox')
    with dbc.write_txn(board), pytest.raises(ValueError, match='drained containment intent'):
        source.prepare(board, task, source='worker-a3', destination='hub', nonce='next-attempt-00001', expected_version=7)
    with dbc.write_txn(board):
        board.execute('UPDATE containment_delivery SET confirmed=1')
        source.prepare(board, task, source='worker-a3', destination='hub', nonce='next-attempt-00001', expected_version=7)
    assert source.frozen(board, task)
