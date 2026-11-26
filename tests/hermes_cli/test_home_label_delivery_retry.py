"""Captured label bytes survive temporary destination custody. [daveey]"""
from copy import deepcopy
from types import SimpleNamespace

from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_home_memo as memo
from hermes_cli import kanban_operator_labels as labels
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_home_review_composed_delivery as cases

board = cases.board


def test_captured_label_retry_never_rebinds_epoch(board, monkeypatch):
    task = cases.support.card(board, 'ready')
    event = 'captured-label-retry-0001'
    cases._label_job(board, task, event)
    sent, epoch = [], ['original-captured-epoch']
    applied = [False]

    def evidence(_board, table, _uid):
        if table == 'tasks':
            return {'state': 'live', 'row': {'id': task, 'home_generation': epoch[0], 'home_transfer_pending': None}}
        return {'state': 'live', 'row': {**sent[-1]['fields'], 'uid': event, 'origin_gateway': 'hub'}}

    def ops(_board, batch):
        sent.extend(deepcopy(batch))
        return {'results': [{'client_op_id': batch[0]['client_op_id'], 'status': 'applied' if applied[0] else 'error'}]}

    peer = SimpleNamespace(row_evidence=evidence, ops=ops, close=lambda: None)
    monkeypatch.setattr(client, 'load_identity', lambda: object())
    monkeypatch.setattr(client, 'Hub', lambda _identity: peer)
    assert labels._deliver(board, 'hub', 'default', now=10) == 0
    original = deepcopy(memo.get(board, event))
    assert original['home_generations'] == {task: epoch[0]}
    cases._label_freeze(board, task, 'pending')
    for now in range(20, 50):
        assert labels._deliver(board, 'hub', 'default', now=now) == 0
    assert len(sent) == 1
    assert cases._job(board)['delivery_attempts'] == 1
    assert memo.get(board, event) == original
    cases.unfreeze(board, task, 'pending')
    epoch[0], applied[0] = 'newer-epoch-must-not-be-borrowed', True
    assert labels._deliver(board, 'hub', 'default', now=60) == 1
    assert sent == [original, original]
    assert cases._job(board)['delivery_attempts'] == 2
    assert cases._job(board)['delivered_at'] is not None
    with dbc.write_txn(board):
        assert memo.get(board, event) == original


def test_frozen_prefix_does_not_consume_delivery_selection_budget(board, monkeypatch):
    frozen = cases.support.card(board, 'ready')
    healthy = cases.support.card(board, 'ready')
    cases._label_job(board, frozen, 'frozen-event-0001')
    with dbc.write_txn(board):
        board.execute("INSERT INTO operator_label_jobs(request_uid,board,machine,labels,caller,result_task_id,"
                      "state,outcome,detail,event_uid,created_at,delivery_attempts) "
                      "VALUES ('healthy','default','worker-a3','[]','{}',?,'done','verified','ok',?,2,0)",
                      (healthy, 'healthy-event-0001'))
    cases._label_freeze(board, frozen, 'pending')
    sent = []
    cases._fake_label_hub(monkeypatch, healthy, 'healthy-event-0001', sent, 'healthy-epoch-0001')
    monkeypatch.setattr(labels, 'MAX_DELIVERIES_PER_TICK', 1)
    assert labels._deliver(board, 'hub', 'default', now=10) == 1
    assert len(sent) == 1 and sent[0]['fields']['task_id'] == healthy
    row = board.execute("SELECT delivery_attempts,delivered_at FROM operator_label_jobs WHERE request_uid='request'").fetchone()
    assert tuple(row) == (0, None)


def test_freeze_after_selection_is_not_an_attempt(board, monkeypatch):
    task = cases.support.card(board, 'ready')
    event = 'selection-race-label-0001'
    cases._label_job(board, task, event)
    original = labels._push_event
    sent = []
    cases._fake_label_hub(monkeypatch, task, event, sent, 'race-epoch-0001')

    def raced(*args):
        cases._label_freeze(board, task, 'pending')
        return original(*args)

    monkeypatch.setattr(labels, '_push_event', raced)
    assert labels._deliver(board, 'hub', 'default', now=10) == 0
    assert cases._job(board)['delivery_attempts'] == 0
    assert not sent and memo.get(board, event) is None
    cases.unfreeze(board, task, 'pending')
    monkeypatch.setattr(labels, '_push_event', original)
    assert labels._deliver(board, 'hub', 'default', now=20) == 1
    assert len(sent) == 1 and sent[0]['home_generations'] == {task: 'race-epoch-0001'}
