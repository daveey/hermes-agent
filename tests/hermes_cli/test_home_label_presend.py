"""Pre-send errors are not historical transmission custody."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_connect as dbc, kanban_sync_client as client
from hermes_cli import kanban_operator_labels as labels
from tests.hermes_cli import test_kanban_home_transfer_fence as support

board = support.board


@pytest.mark.parametrize('fault', ['snapshot', 'pending', 'identity', 'constructor', 'legacy'])
def test_actual_delivery_retry_distinguishes_presend_failure_from_legacy(board, monkeypatch, fault):
    task = support.seed(board)
    event = 'label-presend-event-0001'
    with dbc.write_txn(board):
        for sql in labels.SCHEMA:
            board.execute(sql)
        board.execute("INSERT INTO operator_label_jobs(request_uid,board,machine,labels,caller,result_task_id,"
                      "state,outcome,detail,event_uid,created_at,delivery_attempts) "
                      "VALUES ('request','default','worker-a3','[]','{}',?,'done','verified','ok',?,1,?)",
                      (task, event, int(fault == 'legacy')))
    sent = []
    failing = [fault != 'legacy']
    epoch = ['epoch-before-send-00001']
    def evidence(_board, table, _uid):
        if table == 'task_events':
            return {'state':'live', 'row':{**sent[-1]['fields'], 'uid':event, 'origin_gateway':'hub'}}
        if failing[0] and fault == 'snapshot':
            raise OSError('snapshot unavailable')
        return {'state':'live', 'row':{'id':task, 'home_generation':epoch[0],
                                      'home_transfer_pending':'pending' if failing[0] and fault == 'pending' else None}}
    def send(_board, ops):
        sent.extend(ops)
        return {'results':[{'status':'applied', 'client_op_id':ops[0]['client_op_id']}]}
    hub = SimpleNamespace(row_evidence=evidence, ops=send, close=lambda: None)
    def identity():
        if failing[0] and fault == 'identity':
            raise OSError('identity unavailable')
    def constructor(_identity):
        if failing[0] and fault == 'constructor':
            raise OSError('constructor unavailable')
        return hub
    monkeypatch.setattr(client, 'load_identity', identity)
    monkeypatch.setattr(client, 'Hub', constructor)
    if fault != 'legacy':
        assert labels._deliver(board, 'hub', 'default', now=10) == 0
        assert not sent
        assert board.execute('SELECT delivery_attempts FROM operator_label_jobs').fetchone()[0] == 1
        if fault == 'pending':
            assert labels._deliver(board, 'hub', 'default', now=15) == 0
            assert not sent, 'an attempted pre-send retry bypassed pending validation'
    # Reopen before first capture: producer-version evidence must be durable.
    path = board.execute('PRAGMA database_list').fetchone()[2]
    reopened = dbc.connect(Path(path))
    try:
        failing[0] = False
        assert labels._deliver(reopened, 'hub', 'default', now=20) == 1
        assert sent[0].get('home_generations') == (None if fault == 'legacy' else {task:epoch[0]})
        epoch[0] = 'epoch-after-send-00002'
        labels._push_event(reopened, 'default', 'hub', task, event, json.loads(sent[0]['fields']['payload']))
        assert sent[1] == sent[0]
    finally:
        reopened.close()
