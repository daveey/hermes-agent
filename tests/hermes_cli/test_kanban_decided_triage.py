"""Maintenance finishes recorded decisions, never invents execution authority."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as connect
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from tests.hermes_cli import test_kanban_spend_budget as support
from tests.hermes_cli import test_kanban_scope_renewal as scope

kanban_home = support.kanban_home
board = scope.board


def repair(conn):
    from hermes_cli.kanban_decided_triage import repair_decided_triage
    return repair_decided_triage(conn)


def stopped(conn, cause=None):
    tid = kb.create_task(conn, title='Maintenance decision fixture', assignee='default')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'spend_budget_triaged', {'reason': 'budget used', **({'cause': cause} if cause else {})})
    return tid


def authorize(conn, tid):
    budget.set_override(conn, tid, runs=10, by='default', reason='approved next round', writer_kind='human')


@pytest.mark.parametrize('parent', [False, True])
def test_budget_decision_finishes_once_and_parents_eventually_release(kanban_home, parent):
    with connect.connect_closing() as conn:
        tid = stopped(conn)
        if parent:
            pid = kb.create_task(conn, title='Prerequisite', assignee='default')
            kb.link_tasks(conn, pid, tid)
        authorize(conn, tid)
        assert repair(conn) == [tid]
        assert kb.get_task(conn, tid).status == ('todo' if parent else 'ready')
        assert not decision.pending(conn, tid)
        assert repair(conn) == []
        if parent:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='done' WHERE id=?", (pid,))
            kb.recompute_ready(conn)
            assert kb.get_task(conn, tid).status == 'ready'


@pytest.mark.parametrize('hold', ['undecided', 'scope_stopped', 'safety_refusal', 'human', 'foreign', 'containment'])
def test_independent_holds_survive(kanban_home, monkeypatch, hold):
    with connect.connect_closing() as conn:
        tid = stopped(conn, hold if hold in ('scope_stopped', 'safety_refusal') else None)
        if hold != 'undecided':
            authorize(conn, tid)
        if hold == 'human':
            conn.execute("UPDATE tasks SET human_request='Please decide' WHERE id=?", (tid,))
            conn.commit()
        if hold == 'foreign':
            from hermes_cli import kanban_wake_check
            monkeypatch.setattr(kanban_wake_check, 'foreign_copy', lambda c, t: True)
        if hold == 'containment':
            from hermes_cli import kanban_containment_guard
            monkeypatch.setattr(kanban_containment_guard, 'pending', lambda c, t: True)
        before = conn.total_changes
        assert repair(conn) == []
        assert conn.total_changes == before
        assert kb.get_task(conn, tid).status == 'triage'
        assert decision.pending(conn, tid)


def test_real_authorized_scope_renewal_is_finished(board, monkeypatch):
    tid, _, episode = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    scope.renew(board, tid, episode, scope.mandate(board, tid))
    assert repair(board) == [tid]
    assert kb.get_task(board, tid).status == 'ready'
    assert not decision.pending(board, tid)


@pytest.mark.parametrize('verdict', ['release', 'wait_on', 'archive'])
def test_canonical_reviewer_placement_is_not_repaired_from_partial_feed(board, monkeypatch, verdict):
    from hermes_cli import kanban_scope_renewal as renewal
    tid, _, episode = scope.stopped(board, monkeypatch)
    scope.link(board, tid)
    scope.renew(board, tid, episode, scope.mandate(board, tid))
    event = renewal.renewals(board, tid)[0]
    payload = {**event['payload'], 'authority_kind': 'spend_reviewer', 'verdict': verdict}
    with kb.write_txn(board):
        board.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), event['id']))
    # Feed delivered renewal before canonical placement/dependency edges. Never
    # infer release from it; the canonical operation owns all three verdicts.
    assert renewal.renewals(board, tid)
    before = board.total_changes
    assert repair(board) == []
    assert board.total_changes == before
    assert kb.get_task(board, tid).status == 'triage'


def test_maintenance_hook_handles_fresh_decision_without_stale_sweep(kanban_home):
    from hermes_cli.kanban_triage_sweep import sweep_stale_triage
    with connect.connect_closing() as conn:
        tid = stopped(conn)
        authorize(conn, tid)
        assert sweep_stale_triage(conn, older_than=999999) == []
        assert kb.get_task(conn, tid).status == 'ready'
        assert not decision.pending(conn, tid)


@pytest.mark.parametrize('cause', [[], {}, ['scope_stopped'], {'safety_refusal': True}],
                         ids=['list', 'dict', 'nested-scope', 'nested-safety'])
def test_malformed_cause_retains_hold_and_sweeps_healthy_neighbor(kanban_home, cause):
    from hermes_cli.kanban_triage_sweep import sweep_stale_triage
    with connect.connect_closing() as conn:
        bad = stopped(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, bad, 'spend_budget_triaged', {'cause': cause})
        good = stopped(conn)
        authorize(conn, good)
        assert sweep_stale_triage(conn, older_than=999999) == []
        assert kb.get_task(conn, bad).status == 'triage'
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='promoted_manual'",
                                (bad,)).fetchone()
        assert kb.get_task(conn, good).status == 'ready'
        assert not decision.pending(conn, good)
        with pytest.raises(ValueError, match='cause'):
            decision.pending_episode(conn, bad)


@pytest.mark.parametrize('failure', ['observation', 'revalidation', 'write'])
def test_card_failure_does_not_abort_healthy_neighbor(kanban_home, monkeypatch, caplog, failure):
    from hermes_cli import kanban_decided_triage as maintenance
    from hermes_cli.kanban_triage_sweep import sweep_stale_triage
    with connect.connect_closing() as conn:
        bad = stopped(conn)
        good = stopped(conn)
        for tid in (bad, good):
            authorize(conn, tid)
        original_read, original_append = maintenance._recorded, kb._append_event
        reads = []

        def read(c, tid):
            if tid == bad:
                reads.append(tid)
                if failure == 'observation' or (failure == 'revalidation' and len(reads) == 2):
                    raise RuntimeError('Injected per-card read failure')
            return original_read(c, tid)

        def append(c, tid, kind, payload, **kw):
            result = original_append(c, tid, kind, payload, **kw)
            if tid == bad and kind == 'promoted_manual' and failure == 'write':
                raise RuntimeError('Injected per-card write failure')
            return result

        monkeypatch.setattr(maintenance, '_recorded', read)
        monkeypatch.setattr(kb, '_append_event', append)
        assert sweep_stale_triage(conn, older_than=999999) == []
        assert kb.get_task(conn, bad).status == 'triage'
        assert decision.pending(conn, bad)
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='promoted_manual'",
                                (bad,)).fetchone()
        assert kb.get_task(conn, good).status == 'ready'
        assert not decision.pending(conn, good)
        assert bad in caplog.text
        assert not conn.in_transaction


def test_writer_rechecks_independent_hold(kanban_home, monkeypatch):
    from contextlib import contextmanager
    with connect.connect_closing() as conn:
        tid = stopped(conn)
        authorize(conn, tid)
        original = kb.write_txn

        @contextmanager
        def raced(c, **kw):
            with original(c):
                c.execute("UPDATE tasks SET human_request='New hold' WHERE id=?", (tid,))
            with original(c, **kw):
                yield

        monkeypatch.setattr(kb, 'write_txn', raced)
        assert repair(conn) == []
        assert kb.get_task(conn, tid).status == 'triage'
        assert decision.pending(conn, tid)


def test_human_comment_after_decision_and_old_decision_are_not_released(kanban_home):
    with connect.connect_closing() as conn:
        tid = stopped(conn)
        authorize(conn, tid)
        kb.add_comment(conn, tid, 'human', 'Wait, do not continue', writer_kind='human')
        assert repair(conn) == []
        other = stopped(conn)
        authorize(conn, other)
        with kb.write_txn(conn):
            kb._append_event(conn, other, 'spend_budget_triaged', {'reason': 'new stop'})
        assert repair(conn) == []


def test_human_instruction_between_stop_and_budget_and_agent_budget_hold(kanban_home):
    with connect.connect_closing() as conn:
        tid = stopped(conn)
        kb.add_comment(conn, tid, 'human', 'Do not run this yet; budget preparation only.', writer_kind='human')
        authorize(conn, tid)
        assert repair(conn) == []
        prepared = stopped(conn)
        budget.set_override(conn, prepared, runs=10, by='agent', reason='Prepare budget only', writer_kind='agent')
        before = conn.total_changes
        assert repair(conn) == []
        assert conn.total_changes == before
        assert kb.get_task(conn, tid).status == kb.get_task(conn, prepared).status == 'triage'


def _move(conn, tid, kind, *, shift):
    """Model a synced event whose origin time differs from its local arrival id."""
    (eid,) = conn.execute('SELECT MAX(id) FROM task_events WHERE task_id=? AND kind=?', (tid, kind)).fetchone()
    with kb.write_txn(conn):
        conn.execute('UPDATE task_events SET created_at=created_at+? WHERE id=?', (shift, eid))


def test_reordered_arrival_uses_origin_order(kanban_home):
    with connect.connect_closing() as conn:
        # Decision arrived after the stop locally but was made before it at origin.
        tid = stopped(conn)
        authorize(conn, tid)
        _move(conn, tid, budget.OVERRIDE_EVENT, shift=-100)
        assert repair(conn) == []
        # A human note that arrived last locally but predates the stop at origin
        # is not an instruction since the stop.
        other = stopped(conn)
        authorize(conn, other)
        kb.add_comment(conn, other, 'human', 'Old note from before the stop', writer_kind='human')
        _move(conn, other, 'commented', shift=-100)
        assert repair(conn) == [other]
        assert kb.get_task(conn, other).status == 'ready'


def test_renewal_alone_never_places_exhausted_budget(board, monkeypatch):
    tid, _, episode = scope.stopped(board, monkeypatch)
    support._runs(board, tid, 16)
    scope.link(board, tid)
    scope.renew(board, tid, episode, scope.mandate(board, tid))
    assert budget.over(budget.spend(board, tid), budget.card_limits(board, tid, budget.limits()))
    before = board.total_changes
    assert repair(board) == []
    assert board.total_changes == before
    assert kb.get_task(board, tid).status == 'triage'
    assert decision.pending(board, tid)


def test_renewal_with_budget_waits_for_parent_then_claims(board, monkeypatch):
    from hermes_cli.kanban_spend_claim import claim_check
    tid, _, episode = scope.stopped(board, monkeypatch)
    support._runs(board, tid, 16)
    scope.link(board, tid)
    scope.renew(board, tid, episode, scope.mandate(board, tid))
    budget.set_override(board, tid, runs=30, by='default', reason='More total room', writer_kind='human')
    pid = kb.create_task(board, title='Parent', assignee='default')
    kb.link_tasks(board, pid, tid)
    assert repair(board) == [tid]
    assert kb.get_task(board, tid).status == 'todo'
    assert kb.claim_task(board, tid) is None
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (pid,))
    kb.recompute_ready(board)
    assert kb.get_task(board, tid).status == 'ready'
    assert not claim_check(board, tid)()
    assert kb.claim_task(board, tid) is not None
