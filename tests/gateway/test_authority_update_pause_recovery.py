"""Recovery/authorization and failed-proof authority paths for update checkpoints."""
import asyncio
import threading

import pytest

from gateway.session_authority import LiveSession, SessionAuthority
from hermes_state_runtime import RuntimeStoreError, begin_runtime_epoch, get_session_admission, recover_session_inputs
from hermes_state_update_pause import get_session_checkpoint
from tests.gateway.test_authority_update_pause import parked_fixture, tool_boundary, until
from tests.gateway.test_session_authority_cancel_settlement import ACTOR, REF, _authority, _submit


@pytest.mark.asyncio
async def test_replacement_owner_can_stop_checkpoint_before_scheduling(tmp_path, monkeypatch):
    db, old, row = parked_fixture(tmp_path, monkeypatch)
    with db:
        epoch = begin_runtime_epoch(db, instance_id='replacement')
        recover_session_inputs(db, epoch=epoch)
        owner = SessionAuthority(old.runner, profile_id='owned', instance_id='replacement', db=db, epoch=epoch)
        owner.sessions['s'] = LiveSession(old.sessions['s'].source, 'route')
        await owner.interrupt(ACTOR, REF, row['generation'])
        current = get_session_admission(db, admission_id=row['admission_id'])
        assert current['outcome'] == 'interrupted'
        assert get_session_checkpoint(db, session_id='s') is None
        assert not owner.update_parked


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [OSError('disk'), RuntimeStoreError('invalid_checkpoint')])
async def test_checkpoint_failure_is_interrupted_not_parked_or_completed(tmp_path, monkeypatch, failure):
    from gateway import session_finite, session_update_pause
    db, owner = _authority(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(session_update_pause, 'checkpoint_session_input', fail)
    async def execute(authority, ref, row):
        tool_boundary(db)
        authority.pending_results[row['admission_id']] = {'result': {'paused_for_update': True}, 'usage': {}}
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    with db:
        head = await _submit(owner, 'original')
        await asyncio.wait_for(owner.sessions['s'].task, 5)
        assert get_session_admission(db, admission_id=head.admission_id)['outcome'] == 'interrupted'
        assert get_session_checkpoint(db, session_id='s') is None
        assert not owner.update_parked
        assert not owner.sessions['s'].event_stream.execution


@pytest.mark.asyncio
async def test_resume_reauthorizes_native_route_before_consuming_receipt(tmp_path, monkeypatch):
    from gateway import session_envelope
    from hermes_cli import admission_fence
    db, owner, row = parked_fixture(tmp_path, monkeypatch, payload={'native_text_v1': {'event': {}}, 'text': 'original'})
    checked = []
    async def refuse(*args):
        checked.append(args)
        raise RuntimeStoreError('permission_denied')
    monkeypatch.setattr(session_envelope, 'check_native_route', refuse)
    monkeypatch.setattr(admission_fence, 'is_held', lambda root: False)
    with db:
        with pytest.raises(RuntimeStoreError, match='permission_denied'):
            await owner._claim_next(REF, owner.sessions['s'])
        assert len(checked) == 1
        assert get_session_checkpoint(db, session_id='s')['admission_id'] == row['admission_id']
        assert not owner.executing


@pytest.mark.asyncio
async def test_hold_arriving_during_reauthorization_does_not_consume_checkpoint(tmp_path, monkeypatch):
    from gateway import session_envelope
    from hermes_cli import admission_fence
    db, owner, row = parked_fixture(tmp_path, monkeypatch, payload={'native_text_v1': {'event': {}}, 'text': 'original'})
    held = [False]
    async def reauthorize(*args):
        held[0] = True
    monkeypatch.setattr(session_envelope, 'check_native_route', reauthorize)
    monkeypatch.setattr(admission_fence, 'is_held', lambda root: held[0])
    with db:
        selected, _ = await owner._claim_next(REF, owner.sessions['s'])
        assert selected == 'update_paused'
        assert get_session_checkpoint(db, session_id='s')['admission_id'] == row['admission_id']
        assert not owner.executing


@pytest.mark.asyncio
async def test_completed_checkpoint_does_not_make_followers_update_stoppable(tmp_path, monkeypatch):
    from gateway.session_update_pause import interrupt_update
    from hermes_state_runtime import settle_session_input, admit_session_input
    from hermes_cli import admission_fence
    db, owner, row = parked_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(admission_fence, 'is_held', lambda root: False)
    with db:
        resumed, _ = await owner._claim_next(REF, owner.sessions['s'])
        settle_session_input(db, epoch=owner.epoch, admission_id=row['admission_id'],
                             generation=resumed['generation'], outcome='completed')
        admit_session_input(db, epoch=owner.epoch, principal_id='human', session_id='s',
                            request_id='follower', payload={'text': 'later'})
        follower, _ = await owner._claim_next(REF, owner.sessions['s'])
        owner.executing.pop('s')  # its handler returned; only ordinary settlement remains
        assert not await interrupt_update(owner, REF, follower['generation'])
        assert get_session_admission(db, admission_id=follower['admission_id'])['status'] == 'started'


@pytest.mark.asyncio
async def test_old_generation_stop_during_resume_commit_skips_execution(tmp_path, monkeypatch):
    from gateway import session_finite
    import hermes_state_update_pause
    from hermes_cli import admission_fence
    db, owner, row = parked_fixture(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    resume = hermes_state_update_pause.resume_session_checkpoint
    def delayed_resume(*args, **kwargs):
        result = resume(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(hermes_state_update_pause, 'resume_session_checkpoint', delayed_resume)
    monkeypatch.setattr(admission_fence, 'is_held', lambda root: False)
    calls = []
    async def execute(authority, ref, claimed):
        calls.append(claimed)
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    with db:
        from hermes_state_runtime import admit_session_input
        admit_session_input(db, epoch=owner.epoch, principal_id='human', session_id='s',
                            request_id='follower', payload={'text': 'later'})
        owner._schedule(REF)
        try:
            await until(entered.is_set)
            stopping = asyncio.create_task(owner.interrupt(ACTOR, REF, row['generation']))
            await until(lambda: row['admission_id'] in owner.update_stops)
            release.set()
            await asyncio.wait_for(stopping, 5)
            await asyncio.wait_for(owner.sessions['s'].task, 5)
            assert [claim['request_id'] for claim in calls] == ['follower']
            assert get_session_admission(db, admission_id=row['admission_id'])['outcome'] == 'interrupted'
            with pytest.raises(RuntimeStoreError, match='stale_generation'):
                await owner.interrupt(ACTOR, REF, row['generation'])
        finally:
            release.set()
            task = owner.sessions['s'].task
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
