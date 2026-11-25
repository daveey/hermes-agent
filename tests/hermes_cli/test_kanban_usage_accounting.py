"""Released turns owe accounting before the next ready/review claim. [daveey]"""
import json
import os
from types import SimpleNamespace

import pytest

from gateway import session_kanban
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_spend_budget as budget
from tests.hermes_cli import test_kanban_spend_budget as spending

kanban_home = spending.kanban_home


def released(conn, *, lane='ready', admitted=True):
    tid = kb.create_task(conn, title='isolated missing-result replay', assignee='default')
    if lane == 'review':
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    claim = kb.claim_task if lane == 'ready' else kb.claim_review_task
    assert claim(conn, tid)
    task = kb.get_task(conn, tid)
    context = {'db': str(kb.kanban_db_path()), 'task_id': tid,
               'run_id': task.current_run_id, 'claim_lock': task.claim_lock}
    with kb.write_txn(conn):
        if admitted:
            kb._append_event(conn, tid, 'owner_admitted', {'session_id': 'fixture-session'}, run_id=task.current_run_id)
            kb._append_event(conn, tid, 'worker_bound', {'pid': os.getpid(), 'claim_lock': task.claim_lock},
                             run_id=task.current_run_id)
    dispatch._record_task_failure(conn, tid, '', outcome='released', release_claim=True, end_run=True,
                                  infrastructure=True,
                                  event_payload_extra={'unused_grant': True} if not admitted else None)
    if not admitted:
        with kb.write_txn(conn):
            # Board's explicit unused-grant carrier (the local failure helper
            # intentionally does not forward arbitrary release extras).
            kb._append_event(conn, tid, 'released', {'unused_grant': True}, run_id=task.current_run_id)
    assert kb.get_task(conn, tid).status == lane
    return tid, context


def receipt(context, cache=123, *, partial=False):
    agent = SimpleNamespace(session_input_tokens=10, session_output_tokens=2,
                            session_cache_read_tokens=cache, session_cache_write_tokens=0)
    session_kanban._record_worker_result(context, 0, '', agent=agent,
                                         extra={'continuable': True} if partial else None)


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_released_missing_result_refuses_then_late_real_result_recovers(kanban_home, lane):
    with kbc.connect_closing() as conn:
        tid, context = released(conn, lane=lane)
        claim = kb.claim_task if lane == 'ready' else kb.claim_review_task
        assert claim(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'
        assert kb.get_task(conn, tid).status == lane  # not a new spend stop/reset
        receipt(context)
        assert budget.spend(conn, tid).tokens['cache_read_tokens'] == 123
        assert claim(conn, tid) is not None


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_captured_missing_result_shape_never_admits_silently(kanban_home, lane):
    # Exact release payload shape from the hash-verified historical raw bundle;
    # all task/run/session identities below belong only to the disposable fixture.
    with kbc.connect_closing() as conn:
        tid, context = released(conn, lane=lane)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'released', {'error': None, 'retry_status': 'ready'},
                             run_id=context['run_id'])
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='worker_result'", (tid,)).fetchall()
        claim = kb.claim_task if lane == 'ready' else kb.claim_review_task
        assert claim(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'
        # A delayed authentic bound final, never a backfill from the historical
        # partial source-session observation, is the supported recovery carrier.
        receipt(context, 11034880)
        assert budget.spend(conn, tid).tokens['cache_read_tokens'] == 11034880
        assert claim(conn, tid) is not None


def test_duplicate_and_reordered_results_never_reduce_or_double_count(kanban_home):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        receipt(context, 100)
        receipt(context, 100)
        receipt(context, 50, partial=True)  # reordered lower observation cannot erase finalized usage
        assert budget.spend(conn, tid).tokens['cache_read_tokens'] == 100
        assert kb.claim_task(conn, tid) is not None


@pytest.mark.parametrize('usage', [None, {}, {'cache_read_tokens': True}, {'cache_read_tokens': -1},
                                  {'cache_read_tokens': '123'}])
def test_unknown_usage_is_not_zero(kanban_home, usage):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        with kb.write_txn(conn):
            payload = {} if usage is None else {'usage': usage}
            kb._append_event(conn, tid, 'worker_result', payload, run_id=context['run_id'])
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'


def test_explicit_zero_is_a_real_receipt(kanban_home):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        receipt(context, 0)
        row = conn.execute("SELECT payload FROM task_events WHERE kind='worker_result' AND run_id=?",
                           (context['run_id'],)).fetchone()
        assert json.loads(row[0])['usage']['cache_read_tokens'] == 0
        assert kb.claim_task(conn, tid) is not None


def test_restart_and_renewed_window_do_not_erase_accounting_debt(kanban_home):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'commented', {'writer_kind': 'human'})
        budget.set_override(conn, tid, runs=0, cache_read_tokens=0, by='fixture', reason='disabled limits control')
    with kbc.connect_closing() as conn:
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'
        receipt(context)
        assert kb.claim_task(conn, tid) is not None


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_late_accounting_does_not_release_independent_decision(kanban_home, lane):
    with kbc.connect_closing() as conn:
        tid, context = released(conn, lane=lane)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'spend_budget_triaged', {'reason': 'independent retained decision'})
        receipt(context)
        claim = kb.claim_task if lane == 'ready' else kb.claim_review_task
        assert claim(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'decision_pending'


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_settled_usage_retains_budget_hold_and_neighbor_progress(kanban_home, monkeypatch, lane):
    with kbc.connect_closing() as conn:
        tid, context = released(conn, lane=lane)
        receipt(context)
        monkeypatch.setattr(budget, 'limits', lambda: budget.Limits(runs=100, cache_read_tokens=10))
        claim = kb.claim_task if lane == 'ready' else kb.claim_review_task
        assert claim(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'spend_budget_exhausted'
        neighbor = kb.create_task(conn, title='healthy isolated neighbor', assignee='default')
        assert kb.claim_task(conn, neighbor) is not None


@pytest.mark.parametrize('code', [1, 75, 76, 130])
def test_interrupted_numeric_counters_are_only_lower_bounds(kanban_home, code):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        agent = SimpleNamespace(session_input_tokens=0, session_output_tokens=0,
                                session_cache_read_tokens=123, session_cache_write_tokens=0)
        session_kanban._record_worker_result(context, code, 'fixture request interrupted', agent=agent)
        assert budget.spend(conn, tid).tokens['cache_read_tokens'] == 123
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'


def test_bound_start_failure_proves_no_turn_without_inventing_usage(kanban_home):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        frame = {'policy': {'kanban_json': json.dumps(context)}}
        session_kanban.record_start_failure(frame, RuntimeError('fixture construction failed before any turn'))
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='worker_result' ORDER BY id DESC LIMIT 1",
            (tid,)).fetchone()[0])
        assert payload['usage_no_turn'] is True
        assert 'usage' not in payload
        assert kb.claim_task(conn, tid) is not None


def test_bound_midrun_failure_is_not_no_turn_evidence(kanban_home):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        session_kanban.record_worker_failure({'policy': {'kanban_json': json.dumps(context)}},
                                              RuntimeError('fixture interrupted after possible spend'))
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'


def test_partial_requires_final_and_replaces_once(kanban_home):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        receipt(context, 50, partial=True)
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'
        receipt(context, 100)
        assert budget.spend(conn, tid).tokens['cache_read_tokens'] == 100
        assert kb.claim_task(conn, tid) is not None


@pytest.mark.parametrize('first,second', [(100, 50), (50, 100)])
def test_conflicting_finals_stay_pending_in_both_orders(kanban_home, first, second):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        receipt(context, first)
        receipt(context, second)
        assert budget.spend(conn, tid).tokens['cache_read_tokens'] == 100
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'


def test_wrong_bound_claim_cannot_settle_debt(kanban_home):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        receipt({**context, 'claim_lock': 'wrong:claim'})
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='worker_result'", (tid,)).fetchall()
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'


def test_interrupted_result_transaction_stays_pending_after_reopen(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
    original = kb._append_event

    def interrupted(conn, task_id, kind, payload, **kwargs):
        result = original(conn, task_id, kind, payload, **kwargs)
        if kind == 'worker_result':
            raise RuntimeError('fixture interruption before result transaction commit')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(kb, '_append_event', interrupted)
        with pytest.raises(RuntimeError, match='before result transaction commit'):
            receipt(context, 11034880)
    with kbc.connect_closing() as conn:
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='worker_result'", (tid,)).fetchall()
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'
        receipt(context, 11034880)
        assert budget.spend(conn, tid).tokens['cache_read_tokens'] == 11034880
        assert kb.claim_task(conn, tid) is not None


def test_admission_wins_over_an_unused_marker(kanban_home):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'released', {'unused_grant': True}, run_id=context['run_id'])
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_release_precedes_run_row_closure_publication(kanban_home, lane):
    with kbc.connect_closing() as conn:
        tid, context = released(conn, lane=lane)
        with kb.write_txn(conn):
            # Disposable partial-feed fixture: the task/release event arrived,
            # but its ended_at/status/outcome run-row update has not arrived yet.
            conn.execute("UPDATE task_runs SET status='running',outcome=NULL,ended_at=NULL WHERE id=?",
                         (context['run_id'],))
        claim = kb.claim_task if lane == 'ready' else kb.claim_review_task
        assert claim(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_final_arrives_before_run_closure_cannot_escape_spend(kanban_home, lane):
    with kbc.connect_closing() as conn:
        tid, context = released(conn, lane=lane)
        ended = conn.execute('SELECT ended_at FROM task_runs WHERE id=?', (context['run_id'],)).fetchone()[0]
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET status='running',outcome=NULL,ended_at=NULL WHERE id=?",
                         (context['run_id'],))
        receipt(context, 123)
        claim = kb.claim_task if lane == 'ready' else kb.claim_review_task
        assert claim(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET status='done',outcome='released',ended_at=? WHERE id=?",
                         (ended, context['run_id']))
        assert budget.spend(conn, tid).tokens['cache_read_tokens'] == 123
        assert claim(conn, tid) is not None


@pytest.mark.parametrize('code,continuable', [(75, False), (0, True)])
def test_inconsistent_final_marker_does_not_settle(kanban_home, code, continuable):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'worker_result', {
                'exit_code': code, 'continuable': continuable, 'usage_final': True,
                'usage': dict(input_tokens=1, output_tokens=1, cache_read_tokens=123, cache_write_tokens=0)},
                run_id=context['run_id'])
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'


@pytest.mark.parametrize('code,allowed', [(0, True), (75, False), (130, False)])
def test_legacy_failed_snapshot_is_not_a_final(kanban_home, code, allowed):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'worker_result', {'exit_code': code, 'usage': {'cache_read_tokens': 123}},
                             run_id=context['run_id'])
        assert (kb.claim_task(conn, tid) is not None) is allowed


def test_no_turn_marker_cannot_be_injected_through_result_extras(kanban_home):
    with kbc.connect_closing() as conn:
        tid, context = released(conn)
        session_kanban._record_worker_result(context, 1, 'fixture failure', extra={'usage_no_turn': True})
        assert kb.claim_task(conn, tid) is None
        assert sync.pop_claim_deferral(tid) == 'usage_accounting_pending'


def test_pending_diagnostic_names_original_receipt_not_lease_reset(kanban_home):
    from hermes_cli.kanban_db_dispatch_stuck import stuck_warning
    result = kb.DispatchResult(claim_deferred=[('isolated', 'usage_accounting_pending')])
    text = stuck_warning(6, [result])
    assert 'before the Board lease' in text
    assert 'run UID' in text and 'unknown usage is not zero' in text
    assert 'Do not reset history' in text


def test_unused_preturn_release_keeps_healthy_progress(kanban_home):
    with kbc.connect_closing() as conn:
        tid, _ = released(conn, admitted=False)
        assert kb.claim_task(conn, tid) is not None
