"""Real failed interpreter -> dispatcher stop, without any model/provider call."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures
from tests.hermes_cli import test_kanban_sync_run_rows as transport

board = fixtures.board
kanban_home = transport.kanban_home
svc = transport.svc
live = transport.live


def refused_child(conn, context, monkeypatch):
    source = fixtures._CHILD.replace(
        "_record_worker_result(context, 1, 'fixture worker_receipt_timeout')",
        "_record_worker_result(context, 1, 'fixture refusal', "
        "{'failed': True, 'failure_reason': 'content_policy_blocked', 'failure_retryable': False})")
    assert source != fixtures._CHILD
    monkeypatch.setattr(fixtures, '_CHILD', source)
    return fixtures._failed_child(conn, context)


@pytest.mark.parametrize('observer', [False, True])
@pytest.mark.parametrize('existing', [False, True])
def test_refused_interpreter_stops_once_without_retry(board, monkeypatch, observer, existing):
    conn, context = board
    if existing:
        with kb.write_txn(conn):
            kb._append_event(conn, context['task_id'], decision.EVENT, {'reason': 'existing budget stop'})
    result = refused_child(conn, context, monkeypatch)
    dispatch.detect_crashed_workers(conn, failed_results_only=observer)
    task = kb.get_task(conn, context['task_id'])
    assert task is not None
    assert task.status == 'triage'
    assert task.current_run_id is None
    assert task.claim_lock is None
    assert task.consecutive_failures == 0
    run = conn.execute('SELECT * FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()
    assert run['outcome'] == 'safety_refused'
    assert run['ended_at'] is not None
    assert decision.pending(conn, task.id)
    assert conn.execute('SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?',
                        (task.id, decision.EVENT)).fetchone()[0] == 1
    restricted = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind IN "
                              "('spend_budget_triaged','decision_hold_restricted')", (task.id,)).fetchall()
    assert any(json.loads(r[0]).get('source_result_event_id') == result['id'] for r in restricted)
    notices = conn.execute('SELECT payload FROM task_events WHERE task_id=? AND kind=?',
                           (task.id, decision.NOTICE)).fetchall()
    assert len(notices) == 1
    notice = json.loads(notices[0][0])
    assert notice['cause'] == 'safety_refusal'
    assert 'manually promote' not in decision.notice(notice)
    if notice['producer']['kind'] == 'supervisor':
        supervisor = kb.get_task(conn, notice['producer']['task_id'])
        assert supervisor is not None and 'hermes kanban promote' not in (supervisor.body or '')
    decision.ensure_producer(conn, task.id)
    assert conn.execute('SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind=?',
                        (task.id, decision.NOTICE)).fetchone()[0] == 1
    dispatch.detect_crashed_workers(conn)
    assert kb.get_task(conn, task.id).status == 'triage'
    # A comment or historical review event is not safety clearance or a new grant.
    with kb.write_txn(conn):
        kb._append_event(conn, task.id, 'commented', {'writer_kind': 'human'})
        kb._append_event(conn, task.id, 'promoted_manual', {'reason': 'ordinary administrative action'})
    assert decision.pending(conn, task.id)
    ok, reason = kb.promote_task(conn, task.id, actor='fixture', reason='administrative promotion')
    assert not ok and reason and 'safety' in reason.lower()
    assert kb.get_task(conn, task.id).status == 'triage'


@pytest.mark.parametrize('observer', [False, True])
def test_later_owner_restart_cannot_hide_refusal(board, monkeypatch, observer):
    from tests.hermes_cli.test_kanban_owner_restart_failed_receipt import book, latest as last_receipt
    conn, context = board
    refused_child(conn, context, monkeypatch)
    book(context)
    latest = last_receipt(conn)
    assert 'failure_reason' not in json.loads(latest['payload'])
    dispatch.detect_crashed_workers(conn, failed_results_only=observer)
    task = kb.get_task(conn, context['task_id'])
    assert task is not None and task.status == 'triage'
    stop = decision.pending_episode(conn, task.id)
    assert stop is not None
    source = json.loads(stop['payload'])['source_result_event_id']
    assert source != latest['id']
    assert json.loads(conn.execute('SELECT payload FROM task_events WHERE id=?', (source,)).fetchone()[0])['failure_reason'] == 'content_policy_blocked'
    assert task.consecutive_failures == 0


@pytest.mark.parametrize('observer', [False, True])
def test_primary_refusal_queues_exact_release_without_local_closure(live, kanban_home, svc, monkeypatch, observer):
    from hermes_cli.kanban_sync_wire import translate
    from hermes_cli.kanban_failure_release import queued

    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    tid, uid = transport._claimed(live, kanban_home, svc, 'refused interpreter transport')
    task = kb.get_task(live, tid)
    assert task is not None
    context = {'db': str(kanban_home / 'kanban.db'), 'task_id': tid,
               'run_id': task.current_run_id, 'claim_lock': task.claim_lock, 'board': transport.BOARD}
    refused_child(live, context, monkeypatch)
    dispatch.detect_crashed_workers(live, failed_results_only=observer)
    assert queued(live, task.current_run_id)
    assert kb.get_task(live, tid).status == 'running'
    assert live.execute('SELECT status FROM task_runs WHERE uid=?', (uid,)).fetchone()[0] == 'running'
    releases = live.execute("SELECT * FROM kanban_sync_outbox WHERE op='release'").fetchall()
    assert len(releases) == 1
    wire = translate(dict(releases[0]), '0000000000000-0000-fixture')
    assert wire['run_uid'] == uid
    assert wire['diagnostic']['failure_reason'] == 'content_policy_blocked'
    source = wire['diagnostic']['source_result_event_uid']
    assert live.execute("SELECT kind FROM task_events WHERE uid=?", (source,)).fetchone()[0] == 'worker_result'
    dispatch.detect_crashed_workers(live)
    assert live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE op='release'").fetchone()[0] == 1
