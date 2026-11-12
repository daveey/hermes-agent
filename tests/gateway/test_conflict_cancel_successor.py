"""A head cancelled while its preclaim check conflicts must not strand its queued successor.

The notifier-drain selection resolves the attempted queued identity instead of the FIFO
front. When that identity is no longer queued (a /stop cancel committed from a worker
thread during the async check), the drain re-reads and runs the follower rather than
pausing ``admission_conflict`` with the follower still queued.
"""
import asyncio
import contextlib
import json

import pytest

from hermes_state_runtime import RuntimeStoreError, cancel_session_input, list_session_admissions
from tests.gateway.test_session_authority_cancel_settlement import REF, _authority, _submit
from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_successor_runs_when_conflicted_head_is_cancelled_mid_check(tmp_path, monkeypatch):
    from gateway import session_api_turn, session_finite
    from gateway.config import Platform
    db, authority = _authority(tmp_path, monkeypatch, platform=Platform.API_SERVER)
    executed, ids = [], {}

    def check_api_turn(authority_, ref, payload):
        if payload['text'] == 'stale':
            # A /stop cancel commits from a worker thread while the head's check runs.
            cancel_session_input(db, epoch=authority.epoch, admission_id=ids['stale'])
            authority._settle_cancelled(REF, ids['stale'])
            raise RuntimeStoreError('admission_conflict')
    monkeypatch.setattr(session_api_turn, 'check_api_turn', check_api_turn)

    async def execute(authority, ref, row):
        executed.append(row['payload']['text'])
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)

    with db:
        authority._schedule = lambda ref: None
        ids['stale'] = (await _submit(authority, 'stale')).admission_id
        await _submit(authority, 'follower')
        del authority._schedule
        authority._schedule(REF)
        await asyncio.wait_for(authority.sessions['s'].task, 5)
        statuses = {r['request_id']: (r['status'], r['outcome'])
                    for r in list_session_admissions(db, session_id='s', pending_only=False)}
        assert executed == ['follower'], statuses
        assert statuses['stale'][0] != 'queued' and statuses['follower'][0] != 'queued', statuses


class PumpSpins(AssertionError):
    """The drain re-claimed in a loop that never yields to the event loop."""


def _held_wake_front(db, authority, monkeypatch):
    """A notifier wake held by a core-update drain at the FIFO front: the queued identity the
    claim selects (and the conflict branch must re-read) is the row behind it, not the front.

    Returns the wake row and the drain's claim log. A pump that re-claims without waiting (a
    claim_changed never cleared) never yields to the event loop, so the claim wrapper fails it
    with PumpSpins rather than hanging the test."""
    from gateway import session_authority, session_notifier_drain
    from hermes_state_runtime import admit_session_input
    from tests.hermes_state.test_notifier_claim_eligibility import wake_payload
    monkeypatch.setattr(session_authority, '_CLAIM_RETRY_MIN_S', 60)  # no hold-expiry poll in the window
    monkeypatch.setattr(session_notifier_drain, 'held', lambda: True)
    claims, real_claim = [], authority._claim_next

    async def claim_next(ref, live, attempted=None):
        claims.append(1)
        if len(claims) > 50:
            raise PumpSpins(f'{len(claims)} claims: the notifier-held pump never waits')
        return await real_claim(ref, live, attempted)
    authority._claim_next = claim_next
    identity = json.dumps(['kanban-wake', 'route', 'held'])
    wake = admit_session_input(db, epoch=authority.epoch, session_id='s', principal_id='automation:route',
                               request_id=identity, payload=wake_payload(identity, local=True))
    return wake, claims


async def _stop(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, PumpSpins):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize('settle', ['cancel', 'vanish'])
async def test_successor_behind_held_wake_runs_when_conflicted_row_settles_mid_check(tmp_path, monkeypatch, settle):
    """A held notifier wake leads the FIFO; the human row the claim selects conflicts once, then
    conflicts again while it is settled (cancelled, or gone from the ledger). The drain re-reads
    and runs the next eligible row: it never pauses or refuses on the settled row's verdict."""
    from gateway import session_api_turn, session_finite
    from gateway.config import Platform
    db, authority = _authority(tmp_path, monkeypatch, platform=Platform.API_SERVER)
    executed, ids, paused, checks = [], {}, [], []
    real_pause = authority._pause

    def pause(ref, reason):
        paused.append(reason)
        real_pause(ref, reason)
    authority._pause = pause

    def check_api_turn(authority_, ref, payload):
        checks.append(payload.get('text'))
        if payload.get('text') != 'stale':
            return
        if checks.count('stale') == 1:
            # First verdict: the route moved under the row; nothing has settled it yet.
            raise RuntimeStoreError('admission_conflict')
        # The same row conflicts again and was settled while this check ran: a /stop cancel
        # committed from a worker thread, or the row left the ledger entirely.
        if settle == 'cancel':
            cancel_session_input(db, epoch=authority.epoch, admission_id=ids['stale'])
            authority._settle_cancelled(REF, ids['stale'])
        else:
            db._execute_write(lambda conn: conn.execute(
                'DELETE FROM session_admissions WHERE admission_id=?', (ids['stale'],)))
        raise RuntimeStoreError('admission_conflict')
    monkeypatch.setattr(session_api_turn, 'check_api_turn', check_api_turn)

    async def execute(authority_, ref, row):
        executed.append(row['payload'].get('text'))
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)

    with db:
        authority._schedule = lambda ref: None
        wake, _claims = _held_wake_front(db, authority, monkeypatch)
        ids['stale'] = (await _submit(authority, 'stale')).admission_id
        await _submit(authority, 'follower')
        del authority._schedule
        live = authority.sessions['s']
        authority._schedule(REF)
        try:
            def settled():
                rows = list_session_admissions(db, session_id='s')
                return [r['admission_id'] for r in rows] == [wake['admission_id']]
            await async_wait_until(lambda: (executed == ['follower'] and settled()) or live.task.done(),
                                   timeout=5, what='the next eligible row runs and settles')
            pending = [r['admission_id'] for r in list_session_admissions(db, session_id='s')]
            assert executed == ['follower'], (paused, checks)
            assert paused == [], f'the drain paused behind a settled row: {paused}'
            assert pending == [wake['admission_id']], 'only the held notifier wake stays queued'
            assert not live.task.done(), 'the drain stays parked on the held wake'
        finally:
            await _stop(live.task)


@pytest.mark.asyncio
async def test_notifier_held_pump_waits_instead_of_spinning(tmp_path, monkeypatch):
    """Only held notifier wakes queued: the pump waits on claim_changed (cleared at each claim)
    and must not re-claim in a busy loop while the hold lasts."""
    from gateway.config import Platform
    db, authority = _authority(tmp_path, monkeypatch, platform=Platform.API_SERVER)
    with db:
        _wake, claims = _held_wake_front(db, authority, monkeypatch)
        live = authority.sessions['s']
        authority._schedule(REF)
        try:
            await async_wait_until(lambda: claims, timeout=5, what='the pump claims once')
            for _ in range(50):
                await asyncio.sleep(0)
            assert not live.task.done(), live.task.exception()
            assert len(claims) == 1, f'{len(claims)} claims while only a held notifier wake was queued'
        finally:
            await _stop(live.task)
