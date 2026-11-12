"""A started update checkpoint keeps precedence over the notifier-drain queued selection."""
import json

import pytest

from hermes_state_runtime import admit_session_input
from tests.gateway.test_authority_update_pause import parked_fixture
from tests.gateway.test_session_authority_cancel_settlement import REF
from tests.hermes_state.test_notifier_claim_eligibility import wake_payload


@pytest.mark.asyncio
async def test_checkpoint_resumes_ahead_of_held_notifier_wake(tmp_path, monkeypatch):
    from gateway import session_notifier_drain, session_update_pause
    db, owner, head = parked_fixture(tmp_path, monkeypatch)
    identity = json.dumps(['kanban-wake', 'route', 'one'])
    admit_session_input(db, epoch=owner.epoch, session_id='s', principal_id='automation:route',
                        request_id=identity, payload=wake_payload(identity))
    monkeypatch.setattr(owner, '_schedule', lambda ref: None)
    # Notifier wakes are held, the update-pause fence is not: the checkpoint must resume, and
    # the held wake behind it must not turn the claim into a notifier wait.
    monkeypatch.setattr(session_notifier_drain, 'held', lambda: True)
    monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: False)
    resumed = []

    async def resume(authority, ref, live, first):
        resumed.append(first['admission_id'])
        return first
    monkeypatch.setattr(session_update_pause, 'resume_checkpoint', resume)
    selected = []
    with db:
        row, _first = await owner._claim_next(REF, owner.sessions['s'], selected)
    assert row != 'notifier_held'
    assert resumed == [head['admission_id']]
    assert [r['admission_id'] for r in selected] == [head['admission_id']]


@pytest.mark.asyncio
async def test_update_hold_parks_checkpoint_before_notifier_selection(tmp_path, monkeypatch):
    from gateway import session_notifier_drain, session_update_pause
    db, owner, _head = parked_fixture(tmp_path, monkeypatch)
    identity = json.dumps(['kanban-wake', 'route', 'one'])
    admit_session_input(db, epoch=owner.epoch, session_id='s', principal_id='automation:route',
                        request_id=identity, payload=wake_payload(identity))
    monkeypatch.setattr(owner, '_schedule', lambda ref: None)
    monkeypatch.setattr(session_notifier_drain, 'held', lambda: True)
    monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: True)
    with db:
        row, first = await owner._claim_next(REF, owner.sessions['s'], [])
    assert (row, first) == (session_update_pause.UPDATE_PAUSED, None)
