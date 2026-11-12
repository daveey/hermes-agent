"""Refusing a checkpoint never borrows a queued follower's identity."""
import pytest
from hermes_state_runtime import RuntimeStoreError, admit_session_input, get_session_admission
from tests.gateway.test_authority_update_pause import parked_fixture
from tests.gateway.test_session_authority_cancel_settlement import REF


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['admission_conflict', 'permission_denied', 'not_found', 'invalid_checkpoint'])
async def test_refused_checkpoint_fences_exact_head_not_follower(tmp_path, monkeypatch, reason):
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    follower = admit_session_input(db, epoch=owner.epoch, principal_id='human', session_id='s',
                                   request_id='later', payload={'text': 'later'})
    monkeypatch.setattr(owner, '_schedule', lambda ref: None)
    async def refuse(ref, live, attempted=None):
        raise RuntimeStoreError(reason)
    monkeypatch.setattr(owner, '_claim_next', refuse)
    with db:
        await owner._claim_with_retry(REF, owner.sessions['s'])
        assert get_session_admission(db, admission_id=follower['admission_id'])['status'] == 'queued'
        assert get_session_admission(db, admission_id=head['admission_id'])['status'] == 'unknown'
        # Existing explicit discard unblocks this FIFO without replaying the original input.
        await owner.resolve_unknown_as_owner(REF, head['admission_id'], head['generation'])
        assert get_session_admission(db, admission_id=head['admission_id'])['outcome'] == 'interrupted'
        assert get_session_admission(db, admission_id=follower['admission_id'])['status'] == 'queued'


@pytest.mark.asyncio
async def test_refusal_cannot_fence_uncheckpointed_running_turn(tmp_path, monkeypatch):
    from hermes_state_update_pause import delete_session_checkpoint
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        db._execute_write(lambda conn: delete_session_checkpoint(conn, head['admission_id']))
        async def refuse(ref, live, attempted=None):
            raise RuntimeStoreError('permission_denied')
        monkeypatch.setattr(owner, '_claim_next', refuse)
        await owner._claim_with_retry(REF, owner.sessions['s'])
        assert get_session_admission(db, admission_id=head['admission_id'])['status'] == 'started'


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['generation', 'owner_epoch', 'status'])
async def test_refusal_compare_and_swap_preserves_changed_claim(tmp_path, monkeypatch, changed):
    from gateway.session_update_pause import refuse_checkpoint
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        stale = dict(head)
        if changed == 'status':
            db._execute_write(lambda conn: conn.execute(
                "UPDATE session_admissions SET status='unknown' WHERE admission_id=?", (head['admission_id'],)))
        else:
            stale[changed] += 1
        before = get_session_admission(db, admission_id=head['admission_id'])
        assert await refuse_checkpoint(owner, REF, owner.sessions['s'], stale) is False
        assert get_session_admission(db, admission_id=head['admission_id']) == before
