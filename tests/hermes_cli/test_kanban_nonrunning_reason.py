"""Real refusal producer -> persisted snapshot -> row projection. [daveey]"""
import json
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


@pytest.mark.parametrize('observed_at', [None, 1795738750.5488636, 1795716833.0280812],
                         ids=['live-clock', 'occurrence1021', 'occurrence983'])
def test_real_accounting_refusal_reaches_row_and_expires_without_poll_laundering(kanban_home, observed_at):
    with kbc.connect_closing() as conn:
        tid, context = accounting.released(conn)
        captured = capacity.capture(conn, now=observed_at)
        assert captured is not None
        assert kb.claim_task(conn, tid) is None
        result = dispatch.DispatchResult()
        verdict.claim_refused(result, tid, sync.pop_claim_deferral(tid))
        now = captured['observed_at'] + 1
        assert capacity.record(conn, result, board='default', machine='fixture', now=now, captured=captured)
        payload = conn.execute('SELECT payload FROM kanban_capacity_observation WHERE id=1').fetchone()[0]
        persisted = json.loads(payload)
        # The protocol persists milliseconds. Binary floating point does not
        # guarantee seconds -> milliseconds -> seconds is bit-identical; assert
        # both real boundaries exactly, never a tolerance or a renewed poll time.
        assert persisted['decision_at_ms'] == captured['observed_at'] * 1000
        observed_seconds = persisted['decision_at_ms'] / 1000
        value = row_observation(conn, tid, now + 1)
        # A refused claim proves non-execution, not an otherwise missing lifecycle generation.
        assert value['state'] != 'running'
        reason = value['non_running_reason']
        assert reason['code'] == 'usage_accounting_pending'
        assert reason['disposition'] == 'blocked'
        assert reason['actor'] == 'agent'
        assert 'human_request' not in value
        assert reason['observed_at'] == observed_seconds
        assert reason['valid_until'] == observed_seconds + 180
        stale = row_observation(conn, tid, now + 181)
        assert stale['non_running_reason']['freshness'] == 'unknown'
        assert stale['non_running_reason']['unknown_reason'] == 'dispatcher_evidence_stale'
        assert stale['non_running_reason']['observed_at'] == observed_seconds
        assert conn.execute('SELECT payload FROM kanban_capacity_observation WHERE id=1').fetchone()[0] == payload
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


@pytest.mark.parametrize('code,origin', [('lease_held', 'board'), ('offline', 'local'),
                                         ('gateway_draining', 'local'), ('future_code', 'local')])
def test_typed_refusal_projects_plain_copyable_code(code, origin):
    from copy import deepcopy
    from hermes_cli.execution_observations import nonrunning_reason
    detail = sync.ClaimDeferral(code, origin)
    reason = reasons.describe('claim_deferred', detail)
    assert type(reason['code']) is str
    assert reason['code'] == code
    assert deepcopy(reason) == reason
    reason.update(freshness='current', observed_at=1000, valid_until=1180,
                  evidence_ref='fixture', source_revision='one')
    adapted = nonrunning_reason(reason, now=1001)
    if reason['disposition'] != 'unknown':
        assert adapted['code'] == code
        assert adapted['valid_until'] == 1180
    if code == 'offline':
        assert reason['disposition'] == 'unknown'  # no fabricated Board provenance


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
