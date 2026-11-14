"""Checkpoint cache is only a parked hint, never a replacement for transactional proof."""
from tests.hermes_state.raw_rows import raw_append_message
import pytest
from gateway.session_update_pause import checkpoint_head, resume_checkpoint
from tests.gateway.test_authority_update_pause import parked_fixture
from tests.gateway.test_session_authority_cancel_settlement import REF


@pytest.mark.asyncio
async def test_resume_publishes_zero_parked_before_running(tmp_path, monkeypatch):
    from gateway import session_runtime_workers
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        checkpoint_head(owner, REF)
        reports = []
        monkeypatch.setattr(session_runtime_workers, 'persist_work_count',
                            lambda authority, *args: reports.append(dict(authority.update_parked)))
        resumed = await resume_checkpoint(owner, REF, owner.sessions['s'], head)
        assert resumed['update_resume']['admission_id'] == head['admission_id']
        assert reports and reports[-1] == {}, 'resume must publish the cleared parked count'


@pytest.mark.asyncio
async def test_held_poll_does_not_rehash_but_resume_revalidates(tmp_path, monkeypatch):
    from gateway import session_update_pause
    from hermes_state_runtime import RuntimeStoreError, get_session_admission
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        checkpoint_head(owner, REF)
        monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: True)
        monkeypatch.setattr(session_update_pause, 'get_session_checkpoint',
                            lambda *a, **kw: pytest.fail('held polling rehashed transcript'))
        for _ in range(3):
            assert await owner._claim_next(REF, owner.sessions['s']) == ('update_paused', None)
        # A writer changes the suffix while parked: cache must not authorize resume.
        raw_append_message(db, 's', 'assistant', 'changed tail')
        monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: False)
        with pytest.raises(RuntimeStoreError, match='invalid_checkpoint'):
            await resume_checkpoint(owner, REF, owner.sessions['s'], head)
        assert get_session_admission(db, admission_id=head['admission_id'])['generation'] == head['generation']


@pytest.mark.asyncio
async def test_invalid_receipt_is_unknown_not_stranded_started(tmp_path, monkeypatch):
    from hermes_state_runtime import get_session_admission
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    with db:
        raw_append_message(db, 's', 'assistant', 'changed tail')
        await owner._claim_with_retry(REF, owner.sessions['s'])
        assert get_session_admission(db, admission_id=head['admission_id'])['status'] == 'unknown'


@pytest.mark.asyncio
async def test_cancelled_resume_caller_still_clears_parked_report(tmp_path, monkeypatch):
    import asyncio
    import threading
    from gateway import session_runtime_workers
    import hermes_state_update_pause as store
    from tests.waiting import async_wait_until
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = store.resume_session_checkpoint
    def consume(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(store, 'resume_session_checkpoint', consume)
    with db:
        checkpoint_head(owner, REF)
        task = asyncio.create_task(resume_checkpoint(owner, REF, owner.sessions['s'], head))
        try:
            await async_wait_until(entered.is_set, timeout=5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            release.set()
            await async_wait_until(lambda: not session_runtime_workers.mutation_tasks(owner), timeout=5)
            assert not owner.update_parked, 'cancelled caller left a consumed receipt parked'
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
