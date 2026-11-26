"""Actual failure discriminator, not error prose, owns startup wait semantics."""
import pytest

from hermes_cli import kanban_wait_info as waits


def test_launch_discriminator_is_preserved_without_turn_or_accounting_claim():
    out = waits.describe('triage', park_kind='gave_up', since=100, now=101,
                         park={'trigger_outcome': 'spawn_failed', 'error': 'untrusted detail'})
    assert out['reason_code'] == 'spawn_failed'
    assert out['next_actor'] == 'agent'
    assert 'startup failure' in out['waiting_on']
    assert 'untrusted detail' not in str(out)
    assert 'usage' not in str(out) and 'no_turn' not in str(out)
    assert out['waiting_since'] == 100
    assert out['wait_state'] == 'waiting'


@pytest.mark.parametrize('status,kind,payload', [
    ('triage', 'gave_up', {'error': 'spawn_failed: worker could not be launched'}),
    ('triage', 'gave_up', {'trigger_outcome': 'crashed'}),
    ('triage', 'status', {'trigger_outcome': 'spawn_failed'}),
    ('triage', 'supervisor_escalated', {'trigger_outcome': 'spawn_failed'}),
    ('blocked', 'gave_up', {'trigger_outcome': 'spawn_failed'}),
    ('scheduled', 'gave_up', {'trigger_outcome': 'spawn_failed'}),
])
def test_no_startup_inference_from_other_states_events_or_prose(status, kind, payload):
    out = waits.describe(status, park_kind=kind, park=payload, now=101, since=100)
    assert out.get('reason_code') != 'spawn_failed'
    assert 'startup failure' not in out['waiting_on']


def test_replica_generic_failure_cannot_invent_startup_discriminator():
    out = waits.describe_replica({'status': 'triage', 'run': {'outcome': 'gave_up',
                                  'summary': 'spawn_failed', 'ended_at': 100}}, now=101)
    assert out.get('reason_code') != 'spawn_failed'
    assert 'startup' not in out['waiting_on']


def test_genuine_human_attention_precedes_failure_reason():
    out = waits.describe('triage', human=True, park_kind='gave_up',
                         park={'trigger_outcome': 'spawn_failed'}, now=101, since=100)
    assert out['wait_state'] == 'human'
    assert out.get('reason_code') != 'spawn_failed'
