"""Completed-pass evidence boundaries, not synthetic renderer state. [daveey]"""
import json
import time

import pytest

from hermes_cli import kanban_capacity_evidence as capacity
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_dispatch_verdict as verdict
from hermes_cli import kanban_governors as governors
from hermes_cli import kanban_nonrunning_reason as reasons
from tests.hermes_cli import test_kanban_nonrunning_reason as helpers

kanban_home = helpers.kanban_home


def refused(conn):
    tid, context = helpers.accounting.released(conn)
    captured = capacity.capture(conn)
    assert kb.claim_task(conn, tid) is None
    result = dispatch.DispatchResult()
    verdict.claim_refused(result, tid, sync.pop_claim_deferral(tid))
    return tid, context, result, captured


def test_source_history_work_is_bounded_not_just_returned_rows(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='History work bound', assignee='default')
        def measure():
            steps = []
            conn.set_progress_handler(lambda: steps.append(1) or 0, 100)
            try:
                with helpers.source.transaction(conn):
                    got = helpers.source.assemble(conn, [tid], board='default', gateway='fixture',
                                                  profile='default', observed_at=time.time())
                assert got[tid]['task']['id'] == tid
            finally:
                conn.set_progress_handler(None, 0)
            return len(steps)
        baseline = measure()
        with kb.write_txn(conn):
            for _ in range(3000):
                kb._append_event(conn, tid, 'dispatch_skipped', {'reason': 'capacity'})
        large = measure()
        assert large <= baseline + 300, (baseline, large)


def test_real_completed_dispatch_retains_its_own_refusal(kanban_home):
    with kbc.connect_closing() as conn:
        tid, _, result, captured = refused(conn)
        capacity.observe_dispatch(conn, result, board='default', captured=captured)
        snap = reasons.snapshot(conn)
        with helpers.source.transaction(conn):
            sources = helpers.source.assemble(conn, [tid], board='default', gateway=snap['machine'],
                                              profile='default', observed_at=time.time())
        raw = helpers.source.raw({'id': tid}, sources[tid])
        assert raw['non_running_reason']['code'] == 'usage_accounting_pending'
        assert raw['non_running_reason']['disposition'] == 'blocked'
        phase = helpers.source._phase_raw({'id': tid}, sources[tid])
        assert {k: v for k, v in raw.items() if k != 'non_running_reason'} == phase


def test_older_completed_pass_cannot_replace_newer_evidence(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='ordered', assignee='default')
        now = time.time()
        captured = capacity.capture(conn, now=now)
        fresh = dispatch.DispatchResult(claim_deferred=[(tid, 'usage_accounting_pending')])
        assert capacity.record(conn, fresh, board='default', machine='fixture', now=now, captured=capacity.capture(conn, now=now))
        before = reasons.snapshot(conn)
        old = dispatch.DispatchResult(claim_deferred=[(tid, 'capacity')])
        assert not capacity.record(conn, old, board='default', machine='fixture', now=now - 1)
        assert reasons.snapshot(conn) == before
        assert not capacity.record(conn, old, board='default', machine='fixture', now=now, captured=capacity.capture(conn, now=now))
        assert reasons.snapshot(conn) == before
        assert capacity.record(conn, old, board='default', machine='fixture', now=now + 1, captured=captured)
        assert helpers.row_observation(conn, tid, now + 2)['non_running_reason']['family'] == 'capacity'


@pytest.mark.parametrize('field,value', [
    ('tasks', []), ('observed_at_ms', True), ('valid_until_ms', 'later'),
    ('source_revision', ''), ('evidence_ref', None),
])
def test_malformed_optional_snapshot_is_explicit_unknown(kanban_home, field, value):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='malformed', assignee='default')
        now = time.time()
        capacity.record(conn, dispatch.DispatchResult(claim_deferred=[(tid, 'capacity')]),
                        board='default', machine='fixture', now=now, captured=capacity.capture(conn, now=now))
        snap = reasons.snapshot(conn)
        snap[field] = value
        with kb.write_txn(conn):
            conn.execute('UPDATE kanban_capacity_observation SET payload=?', (json.dumps(snap),))
        raw = helpers.row_observation(conn, tid, now + 1)
        assert raw['non_running_reason']['freshness'] == 'unknown'
        assert raw['non_running_reason']['disposition'] == 'unknown'


def test_changed_placement_invalidates_old_refusal_without_changing_status(kanban_home):
    with kbc.connect_closing() as conn:
        # The accounting fixture deliberately uses the legacy unsynced schema.
        with kb.write_txn(conn):
            conn.execute('ALTER TABLE tasks ADD COLUMN runs_on TEXT')
        tid = kb.create_task(conn, title='placement', assignee='default')
        now = time.time()
        capacity.record(conn, dispatch.DispatchResult(claim_deferred=[(tid, 'labels_mismatch')]),
                        board='default', machine='fixture', now=now, captured=capacity.capture(conn, now=now))
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET runs_on=? WHERE id=?', ('["pool:other"]', tid))
        raw = helpers.row_observation(conn, tid, now + 1)
        assert raw['non_running_reason']['freshness'] == 'unknown'
        assert raw['non_running_reason']['unknown_reason'] == 'dispatcher_lifecycle_changed'


def test_new_event_after_truncated_history_invalidates_reason(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='long history', assignee='default')
        with kb.write_txn(conn):
            for _ in range(140):
                kb._append_event(conn, tid, 'commented', {})
        now = time.time()
        capacity.record(conn, dispatch.DispatchResult(claim_deferred=[(tid, 'capacity')]),
                        board='default', machine='fixture', now=now, captured=capacity.capture(conn, now=now))
        assert helpers.row_observation(conn, tid, now + 1)['non_running_reason']['freshness'] == 'current'
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'status', {'status': 'ready'})
        after = helpers.row_observation(conn, tid, now + 2)['non_running_reason']
        assert after['freshness'] == 'unknown'
        assert after['unknown_reason'] == 'dispatcher_lifecycle_changed'


def test_refusal_must_not_bind_to_replacement_lifecycle(kanban_home):
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute('ALTER TABLE tasks ADD COLUMN runs_on TEXT')
        tid, _, result, captured = refused(conn)
        # Real source refusal happened BEFORE a placement edit. Capturing the
        # edited row later cannot authenticate the decision for the new policy.
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET runs_on=? WHERE id=?', ('["pool:other"]', tid))
        now = time.time()
        capacity.record(conn, result, board='default', machine='fixture', now=now, captured=captured)
        reason = helpers.row_observation(conn, tid, now + 1)['non_running_reason']
        assert reason['freshness'] == 'unknown'
        assert reason['disposition'] == 'unknown'


@pytest.mark.parametrize('change', ['same_status_event', 'placement_aba', 'policy_edit'])
def test_capture_rejects_causal_or_policy_replacement(kanban_home, change):
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute('ALTER TABLE tasks ADD COLUMN runs_on TEXT')
        tid, _, result, captured = refused(conn)
        with kb.write_txn(conn):
            if change == 'same_status_event':
                kb._append_event(conn, tid, 'status', {'status': 'ready'})
            elif change == 'placement_aba':
                conn.execute('UPDATE tasks SET runs_on=? WHERE id=?', ('["other"]', tid))
                kb._append_event(conn, tid, 'edited', {'runs_on': '["other"]'})
                conn.execute('UPDATE tasks SET runs_on=NULL WHERE id=?', (tid,))
            else:
                conn.execute('UPDATE tasks SET title=? WHERE id=?', ('changed policy', tid))
        now = time.time()
        capacity.observe_dispatch(conn, result, board='default', captured=captured)
        snap = reasons.snapshot(conn)
        with helpers.source.transaction(conn):
            sources = helpers.source.assemble(conn, [tid], board='default', gateway=snap['machine'],
                                              profile='default', observed_at=now + 1)
        reason = helpers.source.raw({'id': tid}, sources[tid])['non_running_reason']
        assert reason['freshness'] == 'unknown'
        assert reason['disposition'] == 'unknown'


def test_delayed_publication_never_renews_decision_clock(kanban_home):
    with kbc.connect_closing() as conn:
        tid, _, result, captured = refused(conn)
        now = captured['observed_at']
        capacity.record(conn, result, board='default', machine='fixture', now=now + 200, captured=captured)
        reason = helpers.row_observation(conn, tid, now + 201)['non_running_reason']
        assert reason['freshness'] == 'unknown'
        assert reason['unknown_reason'] == 'dispatcher_evidence_stale'
        assert reason['observed_at'] == (now * 1000) / 1000  # wire ms round-trip, never publication time


def test_missing_evaluated_inputs_cannot_be_bound_at_publication(kanban_home):
    with kbc.connect_closing() as conn:
        tid, _, result, _ = refused(conn)
        now = time.time()
        capacity.record(conn, result, board='default', machine='fixture', now=now)
        reason = helpers.row_observation(conn, tid, now + 1)['non_running_reason']
        assert reason['freshness'] == 'unknown'
        assert reason['disposition'] == 'unknown'


def test_real_dispatch_wrapper_captures_before_evaluation(kanban_home, monkeypatch):
    from hermes_cli import kanban_dispatch_owner
    with kbc.connect_closing() as conn:
        tid, _ = helpers.accounting.released(conn)
        def evaluate(actual, **kwargs):
            assert actual is conn
            assert kb.claim_task(actual, tid) is None
            result = dispatch.DispatchResult()
            verdict.claim_refused(result, tid, sync.pop_claim_deferral(tid))
            return result
        monkeypatch.setattr(dispatch, '_dispatch_once_locked', evaluate)
        monkeypatch.setattr(kanban_dispatch_owner, 'run_owned_tick', lambda conn, tick, *args: tick())
        dispatch.dispatch_once(conn, board='default')
        snap = reasons.snapshot(conn)
        with helpers.source.transaction(conn):
            sources = helpers.source.assemble(conn, [tid], board='default', gateway=snap['machine'],
                                              profile='default', observed_at=time.time())
        reason = helpers.source.raw({'id': tid}, sources[tid])['non_running_reason']
        assert reason['freshness'] == 'current'
        assert reason['code'] == 'usage_accounting_pending'


@pytest.mark.parametrize('code,expected', [
    ('usage_accounting_pending', 'accounting'), ('decision_pending', 'decision'),
    ('spend_policy_unavailable', 'spend_policy'), ('spend_budget_exhausted', 'spend_budget'),
    ('claim_not_acquired', 'unrecorded'), ('future_unknown_gate', 'unrecorded'),
])
def test_governor_never_invents_board_lease_for_local_or_unknown_gate(code, expected):
    assert governors._claim_charge(code)['governor'] == expected


@pytest.mark.parametrize('code', ['offline', 'denied', 'lease_held'])
def test_board_lease_requires_actual_board_origin(code):
    assert reasons.describe('claim_deferred', code)['family'] == 'unknown'
    assert governors._claim_charge(code)['governor'] == 'unrecorded'
