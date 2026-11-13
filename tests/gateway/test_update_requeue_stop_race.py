"""[daveey] t_01a1d4a0ecd9bd54: custody of an unstarted notifier wake that ``requeue_unstarted`` hands back.

t_01a1d49d50e4c656 (ea4f9dc74e5) makes a notifier wake that was claimed before a core-update hold, and whose turn
got the session's lease only after the drain asked turns to pause (David-MBP 2026-11-13 03:07:12 PST, chat
20261026_235722_bef6c6), yield before its turn and go back to its FIFO place. One custody gap remained in that
requeue, found in review of PR #2173 (review 5890417193): a Stop (``interrupt_update``) that latches while the
requeue write runs finds the claim still started and tells its caller the turn stops, but the row ends up queued
and the notice runs after the hold. (The bound-steer gap from the same review landed on main in d495dccb3fe.)
"""
import asyncio
import json
import time

import pytest

from agent.update_pause_cancel import UNSTARTED_KEY
from tests.gateway.test_session_authority_cancel_settlement import REF, _authority
from tests.hermes_state.test_notifier_claim_eligibility import wake_payload


async def _until(predicate):
    from tests.waiting import async_wait_until
    await async_wait_until(predicate, timeout=5)


def _yielded(owner, row):
    owner.pending_results[row['admission_id']] = {
        'result': {'paused_for_update': True, UNSTARTED_KEY: True}, 'usage': {}}


async def _stop_session(authority):
    task = authority.sessions['s'].task
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_latched_during_the_requeue_write_cancels_the_head_and_clears_the_latch(tmp_path, monkeypatch):
    """A Stop that ``interrupt_update`` accepts while the requeue write runs must not leave the notice to run after
    the hold, nor a stale latch behind."""
    import hermes_state_update_pause
    from gateway import session_finite, session_notifier_drain
    from gateway.session_update_pause import interrupt_update
    from hermes_state_runtime import admit_session_input, get_session_admission
    db, authority = _authority(tmp_path, monkeypatch)
    held, calls, stops = [False], [], []
    monkeypatch.setattr(session_notifier_drain, 'held', lambda: held[0])
    loop = asyncio.get_running_loop()
    original = hermes_state_update_pause.requeue_unstarted_input

    def racing_write(*args, **kwargs):
        # Stop lands while the write is in flight: it latches, then waits on the claim gate the requeue holds.
        stops.append(asyncio.run_coroutine_threadsafe(interrupt_update(authority, REF, kwargs['generation']), loop))
        deadline = time.monotonic() + 5
        while kwargs['admission_id'] not in authority.update_stops and time.monotonic() < deadline:
            time.sleep(0.01)
        assert kwargs['admission_id'] in authority.update_stops, 'the Stop never latched'
        return original(*args, **kwargs)
    monkeypatch.setattr(hermes_state_update_pause, 'requeue_unstarted_input', racing_write)

    async def execute(owner, ref, row):
        calls.append(row)
        held[0] = True
        _yielded(owner, row)
        return 'answered'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    identity = json.dumps(['kanban-wake', 'route', 'race'])
    with db:
        head = admit_session_input(db, epoch=authority.epoch, session_id='s', principal_id='automation:route',
                                   request_id=identity, payload=wake_payload(identity, local=True))
        waiter = authority.waiters.setdefault(head['admission_id'], loop.create_future())
        authority._schedule(REF)
        try:
            await _until(lambda: stops and stops[0].done())
            assert stops[0].result() is True  # the caller was told the turn stops ...
            from gateway.session_runtime_workers import mutation_tasks
            await _until(lambda: not mutation_tasks(authority))
            row = get_session_admission(db, admission_id=head['admission_id'])
            assert (row['status'], row['outcome']) == ('terminal', 'cancelled'), row  # ... and it does
            assert await asyncio.wait_for(waiter, 5) is None
            assert head['admission_id'] not in authority.update_stops and not authority.pending_stops
            held[0] = False
            authority.sessions['s'].claim_changed.set()
            await _until(lambda: not mutation_tasks(authority))
            assert len(calls) == 1, 'the stopped notice ran after the hold'
        finally:
            held[0] = False
            await _stop_session(authority)
