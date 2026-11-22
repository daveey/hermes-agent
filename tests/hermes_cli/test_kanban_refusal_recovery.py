"""Refusal custody survives notification failure and uid-less episode upgrades."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_refusal_stop as fixtures

board = fixtures.board


@pytest.mark.parametrize('observer', [False, True])
def test_failed_notice_keeps_stop_and_other_worker_progress(board, monkeypatch, observer):
    conn, context = board
    other = kb.create_task(conn, title='ordinary failed interpreter', assignee='default')
    ordinary = kb.claim_task(conn, other, claimer=kb._claimer_id())
    assert ordinary is not None
    fixtures.fixtures._failed_child(conn, {'db': context['db'], 'task_id': other,
                                         'run_id': ordinary.current_run_id, 'claim_lock': ordinary.claim_lock})
    fixtures.refused_child(conn, context, monkeypatch)
    real_producer = decision.producer

    def broken(*args):
        raise RuntimeError('fixture publication failed')

    monkeypatch.setattr(decision, 'producer', broken)
    dispatch.detect_crashed_workers(conn, failed_results_only=observer)
    stopped = kb.get_task(conn, context['task_id'])
    assert stopped is not None
    assert stopped.status == 'triage' and stopped.current_run_id is None and stopped.claim_lock is None
    run = conn.execute('SELECT * FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    assert run['outcome'] == 'safety_refused' and run['ended_at'] is not None
    assert decision.pending(conn, stopped.id)
    assert kb.get_task(conn, other).status == 'ready'
    assert kb.get_task(conn, other).consecutive_failures == 1
    assert not conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND kind=?',
                            (stopped.id, decision.NOTICE)).fetchall()
    monkeypatch.setattr(decision, 'producer', real_producer)
    decision.ensure_producer(conn, stopped.id)
    decision.ensure_producer(conn, stopped.id)
    notices = conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=?',
                           (stopped.id, decision.NOTICE)).fetchall()
    assert len(notices) == 1 and json.loads(notices[0][0])['cause'] == 'safety_refusal'
    assert decision.pending(conn, stopped.id)


def test_uidless_upgrade_reuses_original_episode_and_notice_route(board, monkeypatch):
    conn, context = board
    tid = context['task_id']
    with kb.write_txn(conn):
        kb._append_event(conn, tid, decision.EVENT, {'reason': 'budget fixture'})
    decision.ensure_producer(conn, tid)
    stop = decision.pending_episode(conn, tid)
    assert not stop.get('uid')
    before = conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]
    prior = json.loads(conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=?',
                                   (tid, decision.NOTICE)).fetchone()[0])
    fixtures.refused_child(conn, context, monkeypatch)
    dispatch.detect_crashed_workers(conn)
    assert decision.pending_episode(conn, tid)['id'] == stop['id']
    notices = [json.loads(r[0]) for r in conn.execute(
        'SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id', (tid, decision.NOTICE))]
    assert len(notices) == 2
    assert notices[-1]['episode'] == str(stop['id'])
    assert notices[-1]['producer'] == prior['producer']
    assert notices[-1]['cause'] == 'safety_refusal'
    assert conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == before
    decision.ensure_producer(conn, tid)
    assert conn.execute('SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?',
                        (tid, decision.NOTICE)).fetchone()[0] == 2
