"""Diagnostic identity is task scope, never a fabricated execution generation."""
from copy import deepcopy

import pytest

from hermes_cli import kanban_nonrunning_reason as reasons


def value():
    return {'version': 1, 'identity': {'kind': 'card', 'gateway': 'fixture', 'profile': 'default',
                                     'board': 'default', 'work_id': 'task'},
            'owner': {'gateway': 'fixture', 'profile': 'default'}, 'state': 'unknown',
            'unknown_reason': 'no_run_identity', 'non_running_reason': {
                **reasons.describe('claim_deferred', 'usage_accounting_pending'),
                'freshness': 'current', 'observed_at': 100, 'valid_until': 280,
                'evidence_ref': 'dispatch:fixture:default:uuid', 'source_revision': 'uuid'}}


def row():
    return dict(id='task', gateway='fixture', profile='default', board='default', status='ready')


def test_no_generation_needed_or_invented():
    raw = value()
    before = deepcopy(raw)
    result = reasons.select([raw], row(), now=101)
    assert result['code'] == 'usage_accounting_pending'
    assert result['freshness'] == 'current'
    assert raw == before
    assert 'generation' not in raw['identity']


@pytest.mark.parametrize('field', ['gateway', 'profile', 'board', 'work_id', 'kind'])
def test_every_scope_field_required(field):
    raw = value()
    raw['identity'].pop(field)
    assert reasons.select([raw], row(), now=101)['freshness'] == 'unknown'
    raw = value()
    raw['identity'][field] = 'foreign'
    assert reasons.select([raw], row(), now=101)['freshness'] == 'unknown'


@pytest.mark.parametrize('now', [99, 280, 999])
def test_independent_clock_expires(now):
    assert reasons.select([value()], row(), now=now)['freshness'] == 'unknown'


@pytest.mark.parametrize('state', ['running', 'done', 'failed', 'cancelled', 'needs_user'])
def test_current_conflicting_phase_blocks_reason(state):
    raw = value()
    other = {**deepcopy(raw), 'state': state, 'observed_at': 101, 'valid_until': 281}
    assert reasons.select([raw, other], row(), now=102)['freshness'] == 'unknown'


@pytest.mark.parametrize('state', ['running', 'done', 'archived', 'unrecognized'])
def test_executing_terminal_and_unknown_statuses_are_excluded(state):
    assert reasons.select([value()], {**row(), 'status': state}, now=101) is None


@pytest.mark.parametrize('state', ['todo', 'scheduled', 'blocked', 'triage', 'review'])
def test_non_ready_status_exposes_unknown_without_borrowing_ready_refusal(state):
    current = {**row(), 'status': state}
    before = deepcopy(current)
    result = reasons.select([value()], current, now=101)
    assert result is not None
    assert result['disposition'] == 'unknown' and result['freshness'] == 'unknown'
    assert result['actor'] == 'unknown'
    assert result['unknown_reason'] == 'lifecycle_reason_unobserved'
    assert result['observed_at'] is None and result['valid_until'] is None
    assert 'usage_accounting_pending' not in str(result)
    assert current == before
    original = {'state': 'unknown', 'observed_at': 90, 'valid_until': 270,
                'identity': {'kind': 'card', 'generation': None}, 'source_revision': 'original'}
    enriched = reasons.enrich(original, current, {})
    assert {k: v for k, v in enriched.items() if k != 'non_running_reason'} == original
    assert enriched['non_running_reason'] == result
    assert 'evidence_ref' not in result


@pytest.mark.parametrize('state', ['ready', 'todo', 'scheduled', 'blocked', 'triage', 'review'])
def test_any_current_run_prevents_nonrunning_diagnostic(state):
    current = {**row(), 'status': state, 'current_run_id': 123}
    assert reasons.select([value()], current, now=101) is None
    raw = {'state': 'unknown'}
    assert reasons.enrich(raw, current, {}) is raw


def test_equal_clock_conflict_is_unknown_and_order_independent():
    raw = value()
    other = deepcopy(raw)
    other['non_running_reason'].update(reasons.describe('claim_deferred', 'capacity'))
    for records in ([raw, other], [other, raw]):
        assert reasons.select(records, row(), now=101)['freshness'] == 'unknown'


def test_newer_unknown_never_resurrects_old_refusal():
    old = value()
    new = deepcopy(old)
    new['non_running_reason'].update(freshness='unknown', observed_at=102)
    for records in ([old, new], [new, old]):
        assert reasons.select(records, row(), now=103)['freshness'] == 'unknown'


def test_owner_conflict_rejected():
    raw = value()
    raw['owner']['gateway'] = 'foreign'
    assert reasons.select([raw], row(), now=101)['freshness'] == 'unknown'
