"""t_01a1d6f94ac774a2: a parked automation turn whose proof dies under the drain must not stall its chat.

David-MBP 2026-11-13 12:13: boot recovery kept a valid checkpoint for a parked Kanban-notice batch, a
passive notice row landed (12:13:06) before the drain resumed it (12:13:15), the resume refused
``invalid_checkpoint``, and ``refuse_checkpoint`` fenced the head ``unknown`` and PAUSED the FIFO. GW-10
settles an unattended unknown, but only at boot or on a later claim, so every turn was refused for
1 h 45 min until the next gateway boot. Now (a) the passive writer is refused while the turn is parked,
so the turn resumes, and (b) a head fenced by the drain still settles when nobody waits on it.
"""
import logging

import pytest

from gateway.session_authority import LiveSession, SessionAuthority
from hermes_state_errors import SessionTurnLeaseLostError
from hermes_state_runtime import (admit_session_input, begin_runtime_epoch, get_session_admission,
                                  recover_session_inputs)
from hermes_state_update_pause import get_session_checkpoint
from tests.gateway.test_authority_update_pause import parked_fixture
from tests.gateway.test_session_authority_cancel_settlement import REF

AUTOMATION = {'text': 'kanban batch', 'local_automation_v1': {'identity': 'kanban-batch-1', 'owner': 's'}}
PERSON = {'text': 'kanban batch'}


async def _after_boot(tmp_path, monkeypatch, payload):
    """Owner 1 parks the head and queues a follower; owner 2 boots and recovers (checkpoint kept)."""
    from gateway import session_finite
    from gateway.session_unattended_unknown import settle_recovered_unattended
    from hermes_cli import admission_fence
    db, old, head = parked_fixture(tmp_path, monkeypatch, payload=payload)
    follower = admit_session_input(db, epoch=old.epoch, principal_id='human', session_id='s',
                                   request_id='follower', payload={'text': 'FOLLOWER'})
    epoch = begin_runtime_epoch(db, instance_id='after-update')
    assert recover_session_inputs(db, epoch=epoch) == 0
    assert settle_recovered_unattended(db, epoch) == 0
    assert get_session_checkpoint(db, session_id='s') is not None
    owner = SessionAuthority(old.runner, profile_id='owned', instance_id='after-update', db=db, epoch=epoch)
    owner.sessions['s'] = LiveSession(old.sessions['s'].source, 'route')
    calls = []

    async def execute(authority, ref, row):
        calls.append('resumed' if row.get('update_resume') else row['payload']['text'])
        return calls[-1]
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    monkeypatch.setattr(admission_fence, 'is_held', lambda root: False)
    return db, owner, head, follower, calls


async def _drain(owner):
    import asyncio
    owner._schedule(REF)
    await asyncio.wait_for(owner.sessions['s'].task, 10)


def _void_proof(db):
    """An unguarded writer's row, raw SQL: the public appenders now refuse while the turn is parked."""
    db._execute_write(lambda c: c.execute(
        "INSERT INTO messages(session_id, role, content, timestamp) "
        "VALUES('s', 'user', 'a row an unguarded writer appended', 1)"))


def _status(db, admission):
    row = get_session_admission(db, admission_id=admission['admission_id'])
    return row['status'], row['outcome']


@pytest.mark.asyncio
async def test_notice_between_boot_recovery_and_resume_is_deferred_and_turn_resumes(tmp_path, monkeypatch):
    """Race regression (acceptance 3): the notice arrives in the window and must not stop the chat."""
    db, owner, head, follower, calls = await _after_boot(tmp_path, monkeypatch, AUTOMATION)
    with db:
        metadata = {'notice_id': 'default:t_x:0-1', 'kanban_event_keys': ['default:1']}
        with pytest.raises(SessionTurnLeaseLostError):
            db.append_kanban_notice('s', 'card t_x done', metadata)
        await _drain(owner)
        assert calls == ['resumed', 'FOLLOWER']
        assert _status(db, head) == ('terminal', 'completed')
        assert _status(db, follower) == ('terminal', 'completed')
        # The deferred notice is delivered on its next pass, after the resumed turn.
        assert db.append_kanban_notice('s', 'card t_x done', metadata) > 0


@pytest.mark.asyncio
async def test_drain_fenced_automation_head_settles_and_follower_runs_in_same_drain(tmp_path, monkeypatch, caplog):
    """Acceptance 2: a proof voided after boot recovery (any writer) fences the automation head; GW-10 settles
    it and the same drain runs the queued follower, with no pause."""
    db, owner, head, follower, calls = await _after_boot(tmp_path, monkeypatch, AUTOMATION)
    with db:
        _void_proof(db)  # voids the proof
        assert get_session_checkpoint(db, session_id='s') is None
        with caplog.at_level(logging.INFO, logger='gateway.session_authority'):
            await _drain(owner)
        assert calls == ['FOLLOWER']
        assert _status(db, head) == ('terminal', 'interrupted')
        assert _status(db, follower) == ('terminal', 'completed')
        assert not [r for r in caplog.records if 'paused' in r.getMessage()]


@pytest.mark.asyncio
async def test_drain_fenced_persons_head_still_pauses_and_says_so(tmp_path, monkeypatch, caplog):
    db, owner, head, follower, calls = await _after_boot(tmp_path, monkeypatch, PERSON)
    with db:
        _void_proof(db)
        with caplog.at_level(logging.INFO, logger='gateway.session_authority'):
            await _drain(owner)
        assert calls == []
        assert _status(db, head) == ('unknown', None)
        assert _status(db, follower) == ('queued', None)
        paused = [r for r in caplog.records if r.getMessage() == 'Session s paused: unknown_execution']
        assert paused and paused[0].levelno == logging.WARNING


@pytest.mark.parametrize('reason', ['session_busy', 'runtime_draining', 'unknown_execution', 'admission_conflict'])
def test_every_pause_is_logged_at_warning(tmp_path, monkeypatch, caplog, reason):
    from tests.gateway.test_session_authority_cancel_settlement import _authority
    db, owner = _authority(tmp_path, monkeypatch)
    with db, caplog.at_level(logging.INFO, logger='gateway.session_authority'):
        owner._pause(REF, reason)
    assert [(r.getMessage(), r.levelno) for r in caplog.records] == [(f'Session s paused: {reason}', logging.WARNING)]
