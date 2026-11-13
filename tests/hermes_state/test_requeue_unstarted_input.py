"""[daveey] t_01a1d49d50e4c656: ``requeue_unstarted_input`` hands a claim that never began its turn back to its FIFO place.

Only an exact, unstarted generation: nothing of the conversation's transcript may postdate the claim, and the old
generation is fenced (its settlement refuses) once the row is queued again.
"""
import json

import pytest

import hermes_state_runtime as rt
from hermes_state import SessionDB
from hermes_state_update_pause import requeue_unstarted_input
from tests.hermes_state.test_notifier_claim_eligibility import wake_payload


@pytest.fixture
def claimed(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session('s', source='test')
    epoch = rt.begin_runtime_epoch(db, instance_id='owner')
    identity = json.dumps(['kanban-wake', 'route', 'one'])
    wake = rt.admit_session_input(db, epoch=epoch, session_id='s', principal_id='automation:route',
                                  request_id=identity, payload=wake_payload(identity, local=True))
    rt.admit_session_input(db, epoch=epoch, session_id='s', principal_id='human', request_id='later',
                           payload={'text': 'later'})
    row = rt.claim_session_input(db, epoch=epoch, session_id='s')
    assert row['admission_id'] == wake['admission_id']
    try:
        yield db, epoch, row
    finally:
        db.close()


def _requeue(db, owner_epoch, row, **overrides):
    return requeue_unstarted_input(db, **{'epoch': owner_epoch, 'admission_id': row['admission_id'],
                                          'generation': row['generation'],
                                          'transcript_mark': row['transcript_mark'], **overrides})


def test_requeued_row_is_the_fifo_head_again_and_its_old_generation_is_fenced(claimed):
    db, epoch, row = claimed
    queued = _requeue(db, epoch, row)
    assert (queued['status'], queued['outcome'], queued['generation']) == ('queued', None, None)
    pending = rt.list_session_admissions(db, session_id='s')
    assert [r['admission_id'] for r in pending] == [row['admission_id'], pending[1]['admission_id']]
    assert [r['status'] for r in pending] == ['queued', 'queued']
    with pytest.raises(rt.RuntimeStoreError) as exc:
        rt.settle_session_input(db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
                                outcome='completed')
    assert exc.value.reason == 'stale_generation'
    again = rt.claim_session_input(db, epoch=epoch, session_id='s')
    assert again['admission_id'] == row['admission_id'] and again['generation'] == row['generation'] + 1


def test_refused_once_the_turn_wrote_its_transcript(claimed):
    db, epoch, row = claimed
    db.append_messages_batch('s', [{'role': 'user', 'content': 'the wake'}])
    with pytest.raises(rt.RuntimeStoreError) as exc:
        _requeue(db, epoch, row)
    assert exc.value.reason == 'invalid_checkpoint'
    assert rt.get_session_admission(db, admission_id=row['admission_id'])['status'] == 'started'


@pytest.mark.parametrize('field,value', [('generation', 99), ('epoch', 0)])
def test_refused_for_another_generation_or_owner(claimed, field, value):
    db, epoch, row = claimed
    with pytest.raises(rt.RuntimeStoreError):
        _requeue(db, epoch, row, **{field: value})
    assert rt.get_session_admission(db, admission_id=row['admission_id'])['status'] == 'started'


def test_refused_after_settlement(claimed):
    db, epoch, row = claimed
    rt.settle_session_input(db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
                            outcome='completed')
    with pytest.raises(rt.RuntimeStoreError) as exc:
        _requeue(db, epoch, row)
    assert exc.value.reason == 'stale_generation'
