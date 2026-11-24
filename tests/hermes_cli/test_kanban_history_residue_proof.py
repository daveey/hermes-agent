"""Contract-shape checks derived from Board history_residue.ts (not a live proof)."""
import json

import pytest

from hermes_cli import kanban_history_residue_source as source

from tests.hermes_cli.test_kanban_history_residue_source import specimen


def proof():
    raw = json.dumps(specimen())
    parent = {'uid': 't_stopped', 'id': 't_stopped', 'version': 7, 'created_on': 'home', 'status': 'triage',
              'current_run_uid': None, 'lease_holder': None, 'lease_run_uid': None,
              'lease_expires': None, 'deleted_at': None, 'delete_pending': 0}
    absence = {'board': 'default', 'table': 'task_events', 'uid': 'event-original',
               'state': 'never_existed', 'floor': 0, 'head': 100, 'row': None, 'tombstone': None}
    receipt = {'version': 1, 'gateway': 'home', 'source_json': raw, 'reason': 'Keep history',
               'disposition': 'retained_unreplayed', 'canonical_acceptance': False,
               'historical_identity_certified': False, 'execution_authority': False,
               'reported_rejection_only': True, 'snapshot_only': True, 'observed_at_ms': 50,
               'observed_head': 100, 'parent': parent.copy(), 'absence': absence.copy()}
    current = {'observed_at_ms': 90, 'observed_head': 100, 'parent': parent.copy(), 'absence': absence.copy(),
               'no_lease': True, 'no_running_run': True, 'no_retained_op_receipt': True,
               'op_history_certified': False, 'retained_receipt_floor1': None, 'execution_authority': False}
    return raw, {'version': 1, 'gateway': 'home', 'receipt': receipt, 'current': current}


def test_separates_historical_receipt_from_current_safe_parent_and_cache_uncertainty():
    raw, data = proof()
    data['current']['parent']['version'] = 8
    data['current']['retained_receipt_floor1'] = '0000000000000123'
    validated = source.validate_current(json.dumps(data), raw, gateway='home', board='default', head=100)
    assert validated == data['current']
    assert validated['op_history_certified'] is False


@pytest.mark.parametrize('fault', ['source-byte', 'gateway', 'version', 'head', 'head-bool', 'board', 'event',
    'snapshot', 'acceptance', 'identity-certified', 'execution', 'reported', 'lease', 'run',
    'op-retained', 'op-history', 'active', 'pointer', 'lease-holder', 'lease-run', 'lease-expiry',
    'deleted', 'delete-pending', 'owner', 'task', 'absence-state', 'floor', 'row', 'tombstone'])
def test_changed_or_misleading_proof_cannot_be_classified(fault):
    raw, data = proof()
    current, receipt = data['current'], data['receipt']
    if fault == 'source-byte': receipt['source_json'] += ' '
    if fault == 'gateway': data['gateway'] = 'other'
    if fault == 'version': data['version'] = True
    if fault == 'head': current['observed_head'] = 101
    if fault == 'head-bool': current['observed_head'] = True
    if fault == 'board': current['absence']['board'] = 'other'
    if fault == 'event': current['absence']['uid'] = 'other'
    receipt_flags = {'snapshot': 'snapshot_only', 'acceptance': 'canonical_acceptance',
                     'identity-certified': 'historical_identity_certified', 'execution': 'execution_authority',
                     'reported': 'reported_rejection_only'}
    if fault in receipt_flags: receipt[receipt_flags[fault]] = not receipt[receipt_flags[fault]]
    flags = {'lease': 'no_lease', 'run': 'no_running_run', 'op-retained': 'no_retained_op_receipt', 'op-history': 'op_history_certified'}
    if fault in flags: current[flags[fault]] = not current[flags[fault]]
    patches = {'active': {'status': 'ready'}, 'pointer': {'current_run_uid': 'run'},
               'lease-holder': {'lease_holder': 'other'}, 'lease-run': {'lease_run_uid': 'run'},
               'lease-expiry': {'lease_expires': 1}, 'deleted': {'deleted_at': 1},
               'delete-pending': {'delete_pending': 1}, 'owner': {'created_on': 'other'}, 'task': {'uid': 'other'}}
    current['parent'].update(patches.get(fault, {}))
    if fault == 'absence-state': current['absence']['state'] = 'unknown'
    if fault == 'floor': current['absence']['floor'] = 1
    if fault == 'row': current['absence']['row'] = {}
    if fault == 'tombstone': current['absence']['tombstone'] = {}
    with pytest.raises(source.Refused):
        source.validate_current(json.dumps(data), raw, gateway='home', board='default', head=100)
