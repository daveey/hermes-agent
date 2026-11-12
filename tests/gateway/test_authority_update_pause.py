"""Authority parks a complete tool boundary without settling its FIFO admission."""
import asyncio
import threading

import pytest

from hermes_state_runtime import get_session_admission
from hermes_state_update_pause import get_session_checkpoint
from tests.gateway.test_session_authority_cancel_settlement import ACTOR, REF, _authority, _submit


def tool_boundary(db):
    db.append_messages_batch('s', [
        {'role': 'user', 'content': 'original'},
        {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'call-a', 'type': 'function', 'function': {'name': 'example', 'arguments': '{}'}}]},
        {'role': 'tool', 'content': 'done', 'tool_call_id': 'call-a', 'tool_name': 'example'},
    ])


def parked_fixture(tmp_path, monkeypatch, *, payload=None):
    from hermes_state_runtime import admit_session_input, claim_session_input
    from hermes_state_update_pause import checkpoint_session_input
    db, owner = _authority(tmp_path, monkeypatch)
    admit_session_input(db, epoch=owner.epoch, principal_id='human', session_id='s',
                        request_id='original', payload=payload or {'text': 'original'})
    row = claim_session_input(db, epoch=owner.epoch, session_id='s')
    tool_boundary(db)
    checkpoint_session_input(db, epoch=owner.epoch, admission_id=row['admission_id'],
        generation=row['generation'], physical_session_id='s', transcript_mark=row['transcript_mark'])
    return db, owner, row


async def until(predicate):
    from tests.waiting import async_wait_until
    await async_wait_until(predicate, timeout=5)


@pytest.mark.asyncio
async def test_pause_preserves_waiter_then_resumes_same_admission_before_follower(tmp_path, monkeypatch):
    from gateway import session_finite
    from hermes_cli import admission_fence

    db, authority = _authority(tmp_path, monkeypatch)
    held, calls = [True], []
    monkeypatch.setattr(admission_fence, 'is_held', lambda root: held[0])

    async def execute(owner, ref, row):
        calls.append(row)
        if len(calls) == 1:
            tool_boundary(db)
            owner.pending_results[row['admission_id']] = {'result': {'paused_for_update': True}, 'usage': {}}
            return None
        return 'resumed' if row.get('update_resume') else 'follower'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    frames = []
    authority.sessions['s'].event_stream.observers.add(frames.append)
    with db:
        head = await _submit(authority, 'original')
        waiter = authority.waiters.setdefault(head.admission_id, asyncio.get_running_loop().create_future())
        await _submit(authority, 'later')
        try:
            await until(lambda: get_session_checkpoint(db, session_id='s') is not None or waiter.done())
            assert not waiter.done(), 'pause must not terminalize the original waiter'
            await until(lambda: not authority.sessions['s'].event_stream.execution)
            assert authority.update_parked['s']['admission_id'] == head.admission_id
            from gateway.session_runtime_workers import executing_sessions, mutation_tasks
            await until(lambda: not mutation_tasks(authority))
            assert list(executing_sessions(authority)) == []
            assert get_session_admission(db, admission_id=head.admission_id)['status'] == 'started'
            assert len(calls) == 1
            assert not any(f['params']['type'] == 'message.complete' for f in frames)
            held[0] = False
            await asyncio.wait_for(authority.sessions['s'].task, 5)
            assert await waiter == 'resumed'
            assert [r['request_id'] for r in calls] == ['original', 'original', 'later']
            assert calls[1]['generation'] == calls[0]['generation'] + 1
            assert calls[1]['update_resume']['admission_id'] == head.admission_id
            assert not authority.update_parked
        finally:
            held[0] = False
            task = authority.sessions['s'].task
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('when', ['before', 'during', 'committed', 'after'])
async def test_stop_beats_checkpoint_and_never_resumes(tmp_path, monkeypatch, when):
    from gateway import session_finite, session_update_pause
    from hermes_cli import admission_fence

    db, authority = _authority(tmp_path, monkeypatch)
    authority.runner._resident_agent_for = lambda route: None
    monkeypatch.setattr(admission_fence, 'is_held', lambda root: True)
    barrier, release = threading.Event(), threading.Event()
    returned, unwind = asyncio.Event(), asyncio.Event()
    original = session_update_pause.checkpoint_session_input
    calls = []

    def checkpoint(*args, **kwargs):
        if when == 'during':
            barrier.set()
            assert release.wait(5)
        result = original(*args, **kwargs)
        if when == 'committed':
            barrier.set()
            assert release.wait(5)
        return result
    monkeypatch.setattr(session_update_pause, 'checkpoint_session_input', checkpoint)

    async def execute(owner, ref, row):
        calls.append(row)
        tool_boundary(db)
        owner.pending_results[row['admission_id']] = {'result': {'paused_for_update': True}, 'usage': {}}
        returned.set()
        if when == 'before':
            await unwind.wait()
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    with db:
        head = await _submit(authority, 'original')
        waiter = authority.waiters.setdefault(head.admission_id, asyncio.get_running_loop().create_future())
        try:
            await returned.wait()
            generation = authority._handle(REF).execution_generation
            if when in {'during', 'committed'}:
                await until(barrier.is_set)
                assert authority.sessions['s'].event_stream.execution['admission_id'] == head.admission_id
                assert not authority.update_parked, 'commit follow-up still owns the active stamp'
            if when == 'after':
                await until(lambda: bool(getattr(authority, 'update_parked', {})) or waiter.done())
                assert not waiter.done(), 'must still be unfinished when Stop arrives'
            stopping = asyncio.create_task(authority.interrupt(ACTOR, REF, generation))
            await until(lambda: head.admission_id in authority.update_stops)
            release.set()
            unwind.set()
            await asyncio.wait_for(stopping, 5)
            await asyncio.wait_for(authority.sessions['s'].task, 5)
            assert get_session_admission(db, admission_id=head.admission_id)['outcome'] == 'interrupted'
            assert get_session_checkpoint(db, session_id='s') is None
            assert not authority.update_parked
            assert len(calls) == 1
            assert waiter.done()
        finally:
            release.set()
            unwind.set()
            task = authority.sessions['s'].task
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
