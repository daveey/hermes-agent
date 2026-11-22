"""Reviewer probe: cost and reach of protect_stopped_event_insert on an unsynced board."""
import sqlite3
import time

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_protect_stopped_local as local

board = local.board


def test_insert_trigger_plan_and_cost(board, monkeypatch):
    from hermes_cli import kanban_protect_stopped as protect
    _, _, session, target, _, args = local.fixture(board, monkeypatch)
    other = kb.create_task(board, title='unrelated card', assignee='default')
    idx = [tuple(r)[1:3] for r in board.execute("PRAGMA index_list(task_events)")]
    print('PROBE3 task_events indexes', idx)
    for name, _u in idx:
        print('PROBE3 index', name, [r[2] for r in board.execute(f"PRAGMA index_info({name})")])
    with kb.write_txn(board):
        board.executemany("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                          [(other, 'note', '{}', 1)] * 200000)
    def cost(label):
        t = time.perf_counter()
        with kb.write_txn(board):
            for _ in range(200):
                kb._append_event(board, other, 'note', {})
        print('PROBE3', label, '200 appends ms', round((time.perf_counter() - t) * 1000, 1))
    cost('before protection')
    with scoped_current_session_id(session):
        protect.protect(board, board='default', **args)
    cost('after protection')
    sql = board.execute("SELECT sql FROM sqlite_master WHERE name='protect_stopped_event_insert'").fetchone()[0]
    print('PROBE3 trigger', sql[:200])
    for row in board.execute("EXPLAIN QUERY PLAN SELECT 1 FROM task_events AS o WHERE (o.id=? OR o.uid=?)", (1, 'x')):
        print('PROBE3 plan', tuple(row))
    assert decision.execution_restricted(board, target)
    plan = [r[3] for r in board.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM task_events AS o WHERE (o.id=? OR o.uid=?)", (1, 'x'))]
    assert not any('SCAN' in step for step in plan), plan
    assert any('uid' in step for step in plan), plan
    from hermes_cli import kanban_protect_guards
    before = board.total_changes
    with kb.write_txn(board):
        kanban_protect_guards.install(board)
    assert board.total_changes == before
    receipt = dict(board.execute("SELECT * FROM task_events WHERE kind='decision_protected_stopped'").fetchone())
    receipt['id'] += 1000000  # UID-only arm, not the original primary key.
    receipt['kind'], receipt['payload'] = 'note', '{}'
    columns = list(receipt)
    with pytest.raises(sqlite3.IntegrityError, match='protect_evidence_immutable'):
        board.execute(f"INSERT OR REPLACE INTO task_events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                      tuple(receipt[c] for c in columns))
    assert decision.execution_restricted(board, target)
