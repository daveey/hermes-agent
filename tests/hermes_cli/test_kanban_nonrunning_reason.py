"""Real refusal producer -> persisted snapshot -> row projection. [daveey]"""
import time

import pytest

from hermes_cli import kanban_capacity_evidence as capacity
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_dispatch_verdict as verdict
from hermes_cli import kanban_execution_source as source
from hermes_cli import kanban_nonrunning_reason as reasons
from tests.hermes_cli import test_kanban_usage_accounting as accounting

kanban_home = accounting.kanban_home


def row_observation(conn, tid, now):
    with source.transaction(conn):
        captured = source.assemble(conn, [tid], board='default', gateway='fixture', profile='default', observed_at=now)
    return source.raw({'id': tid}, captured[tid])


@pytest.mark.parametrize('paused,held,code', [(True, False, 'gateway_draining'), (False, True, 'update_admission_hold')])
def test_real_gateway_drain_preserves_refusal_reason(kanban_home, monkeypatch, paused, held, code):
    from hermes_cli import kanban_gateway_drain as drain
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'resolve_profile_env', lambda _: None)
    monkeypatch.setattr(drain, 'dispatch_paused', lambda _: paused)
    monkeypatch.setattr(drain, 'core_update_hold', lambda: {} if held else None)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='draining', assignee='default')
        captured = capacity.capture(conn)
        assert drain.claim_if_admitting(conn, tid, 'default', 'ready', 60) is None
        result = dispatch.DispatchResult()
        verdict.claim_refused(result, tid, sync.pop_claim_deferral(tid))
        now = time.time()
        capacity.record(conn, result, board='default', machine='fixture', now=now, captured=captured)
        value = row_observation(conn, tid, now + 1)
        assert value['non_running_reason']['code'] == code
        assert value['non_running_reason']['family'] == 'drain'
        assert value['non_running_reason']['disposition'] == 'waiting'


def test_real_accounting_refusal_reaches_row_and_expires_without_poll_laundering(kanban_home):
    with kbc.connect_closing() as conn:
        tid, context = accounting.released(conn)
        captured = capacity.capture(conn)
        assert kb.claim_task(conn, tid) is None
        result = dispatch.DispatchResult()
        verdict.claim_refused(result, tid, sync.pop_claim_deferral(tid))
        now = time.time()
        assert capacity.record(conn, result, board='default', machine='fixture', now=now, captured=captured)
        value = row_observation(conn, tid, now + 1)
        # A refused claim proves non-execution, not an otherwise missing lifecycle generation.
        assert value['state'] != 'running'
        reason = value['non_running_reason']
        assert reason['code'] == 'usage_accounting_pending'
        assert reason['disposition'] == 'blocked'
        assert reason['actor'] == 'agent'
        assert 'human_request' not in value
        assert reason['observed_at'] == captured['observed_at']
        stale = row_observation(conn, tid, now + 181)
        assert stale['non_running_reason']['freshness'] == 'unknown'
        assert stale['non_running_reason']['unknown_reason'] == 'dispatcher_evidence_stale'
        assert stale['non_running_reason']['observed_at'] == captured['observed_at']
        accounting.receipt(context)
        assert kb.claim_task(conn, tid) is not None
        current = row_observation(conn, tid, now + 2)
        assert 'non_running_reason' not in current


@pytest.mark.parametrize('code,disposition,family', [
    ('usage_accounting_pending', 'blocked', 'accounting'),
    ('decision_pending', 'blocked', 'decision'),
    ('capacity', 'waiting', 'capacity'),
    ('waiting_for_worker_capacity', 'waiting', 'capacity'),
    ('worker_draining', 'waiting', 'drain'),
    ('update_admission_unavailable', 'blocked', 'drain'),
    ('claim_not_acquired', 'unknown', 'unknown'),
    ('new_unregistered_reason', 'unknown', 'unknown'),
])
def test_reason_registry_never_invents_board_or_human_ownership(code, disposition, family):
    value = reasons.describe('claim_deferred', code)
    assert value['disposition'] == disposition
    assert value['family'] == family
    assert value['actor'] != 'human'


def test_new_ready_without_dispatcher_evidence_is_unknown(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='unobserved', assignee='default')
        value = row_observation(conn, tid, time.time())
        assert value['non_running_reason']['freshness'] == 'unknown'
        assert value['non_running_reason']['unknown_reason'] == 'dispatcher_evidence_missing'


@pytest.mark.parametrize('kwargs', [{'at_capacity': 'max_spawn=1/1'}, {'memory_pressure': 'critical'}])
def test_machine_restriction_does_not_claim_task_or_fleet_eligibility(kanban_home, kwargs):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='not necessarily local', assignee='default')
        now = time.time()
        captured = capacity.capture(conn, now=now)
        capacity.record(conn, dispatch.DispatchResult(**kwargs), board='default', machine='fixture', now=now, captured=captured)
        reason = row_observation(conn, tid, now + 1)['non_running_reason']
        assert reason['scope'] == 'observing_dispatcher'
        assert reason['observer'] == 'fixture'
        assert reason['summary'].startswith('Observing dispatcher: ')


@pytest.mark.parametrize('code', ['capacity', 'decision_pending'])
def test_snapshot_reason_is_fenced_by_lifecycle_not_card_read_time(kanban_home, code):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='one', assignee='default')
        now = time.time()
        captured = capacity.capture(conn, now=now)
        result = dispatch.DispatchResult(claim_deferred=[(tid, code)])
        capacity.record(conn, result, board='default', machine='fixture', now=now, captured=captured)
        before = row_observation(conn, tid, now + 1)
        assert before['non_running_reason']['code'] == code
        # A real successful claim invalidates the previous refusal, not just a UI relabel.
        assert kb.claim_task(conn, tid)
        after = row_observation(conn, tid, now + 2)
        assert 'non_running_reason' not in after
