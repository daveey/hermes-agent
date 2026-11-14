"""Successful receipts are exact-event evidence, not generic task acknowledgement."""
import json

import pytest

from gateway.session_results import finish_result
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input
from hermes_state_terminal import RESULT_PREFIX
from tui_gateway.kanban_remote_receipts import completed_notice

KEY = 'remote:worker:default/default/t_0000000000000001:123'


@pytest.fixture
def receipt(tmp_path):
    with SessionDB(tmp_path / 'state.db') as db:
        db.create_session('chat', 'desktop')
        db.create_session('foreign', 'desktop')
        epoch = begin_runtime_epoch(db, instance_id='owner')
        identity = json.dumps(['kanban-wake', 'route', 'event'])
        payload = {'text': 'one card', 'local_automation_v1': {
            'identity': identity, 'owner': 'chat', 'route': 'route', 'target': 'chat',
            'display_kind': 'kanban_notice', 'kanban_event_keys': [KEY]}}
        admit_session_input(db, epoch=epoch, principal_id='automation:route', session_id='chat',
                            request_id=identity, payload=payload)
        row = claim_session_input(db, epoch=epoch, session_id='chat')
        finish_result(db, epoch=epoch, row=row, response='handled', outcome='completed', result={
            'result': {'completed': True, 'agent_persisted': True, 'final_response': 'handled', 'messages': []}})
        yield db, row, payload


def test_exact_event_and_conversation(receipt):
    db, _, _ = receipt
    assert completed_notice(db, 'chat', KEY)
    assert not completed_notice(db, 'foreign', KEY)
    assert not completed_notice(db, 'chat', KEY.replace(':123', ':124'))
    assert not completed_notice(db, 'chat', '')
    assert not completed_notice(object(), 'chat', KEY)


@pytest.mark.parametrize('field,value', [
    ('status', 'queued'), ('status', 'started'), ('status', 'unknown'),
    ('outcome', 'cancelled'), ('outcome', 'failed'), ('outcome', 'interrupted'), ('outcome', 'rejected'),
    ('principal_id', 'human'), ('request_id', json.dumps(['plugin', 'route', 'event'])),
    ('request_id', '{}'), ('request_id', 'not-json'), ('request_id', json.dumps(['kanban-wake', 'other', 'event'])),
])
def test_unproven_admission_never_acknowledges(receipt, field, value):
    db, row, _ = receipt
    db._execute_write(lambda conn: conn.execute(
        f'UPDATE session_admissions SET {field}=? WHERE admission_id=?', (value, row['admission_id'])))
    assert not completed_notice(db, 'chat', KEY)


@pytest.mark.parametrize('field,value', [('route', 'foreign'), ('owner', 'foreign'),
                                         ('identity', 'different'), ('display_kind', 'user')])
def test_envelope_binding_required(receipt, field, value):
    db, row, payload = receipt
    payload['local_automation_v1'][field] = value
    db._execute_write(lambda conn: conn.execute(
        'UPDATE session_admissions SET payload_json=? WHERE admission_id=?',
        (json.dumps(payload), row['admission_id'])))
    assert not completed_notice(db, 'chat', KEY)


@pytest.mark.parametrize('result', [None, [], {'result': []}, {'result': {}},
    *({'result': {'completed': True, 'agent_persisted': True, field: value}} for field, value in [
        ('completed', False), ('completed', 1), ('agent_persisted', False), ('agent_persisted', 1),
        ('failed', True), ('interrupted', True), ('error', 'failure'), ('paused_for_update', True)])])
def test_result_proof_required(receipt, result):
    db, row, _ = receipt
    db._execute_write(lambda conn: conn.execute(
        'UPDATE state_meta SET value=? WHERE key=?', (json.dumps(result), RESULT_PREFIX + row['admission_id'])))
    assert not completed_notice(db, 'chat', KEY)


def test_missing_receipt_is_not_delivery(receipt):
    db, row, _ = receipt
    db._execute_write(lambda conn: conn.execute('DELETE FROM state_meta WHERE key=?',
                                               (RESULT_PREFIX + row['admission_id'],)))
    assert not completed_notice(db, 'chat', KEY)


def test_matching_non_kanban_producer_is_not_delivery(receipt):
    db, row, payload = receipt
    identity = json.dumps(['plugin', 'route', 'event'])
    payload['local_automation_v1']['identity'] = identity
    db._execute_write(lambda conn: conn.execute(
        'UPDATE session_admissions SET request_id=?,payload_json=? WHERE admission_id=?',
        (identity, json.dumps(payload), row['admission_id'])))
    assert not completed_notice(db, 'chat', KEY)


def test_message_metadata_cannot_supply_receipt(receipt):
    db, row, _ = receipt
    db._execute_write(lambda conn: conn.execute('DELETE FROM state_meta WHERE key=?',
                                               (RESULT_PREFIX + row['admission_id'],)))
    db.append_message('chat', 'user', json.dumps({'local_automation_v1': {'kanban_event_keys': [KEY]},
                                                 'completed': True, 'agent_persisted': True}))
    assert not completed_notice(db, 'chat', KEY)
