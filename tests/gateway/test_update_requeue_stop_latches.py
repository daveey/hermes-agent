"""[daveey] t_01a1d5ada30e3458: clauses of ``_stopped_after_requeue`` (PR #2173) that no test reached.

Post-merge mutants of #2173 (check t_01a1d59498c0a89a) survived on two clauses of the Stop-during-requeue
settlement: clearing a ``pending_stops`` latch (a shutdown Stop, ``stop_authority_turns``, latches the
generation there while the requeue write runs, since the turn has returned and adopted no agent), and
releasing the cancelled head's retained media.

A third survivor, ``update_stops.discard(admission_id)`` in ``_stopped_after_requeue`` alone, is redundant on
every reachable path, so no test here targets it: ``park_or_interrupt`` records the claim in ``update_claims``
(``remember_claim``) under the claim gate and mutation lock it holds through the whole requeue, so
``requeue_unstarted``'s ``update_claims.pop`` always returns this admission and discards its latch, with no
await between that discard and ``_stopped_after_requeue``. The execution-stamp lookup in ``interrupt_update``
(the claim missing from ``update_claims``) is never what latches a Stop during that window.
"""
import asyncio
import json

import pytest

from tests.gateway.test_session_authority_cancel_settlement import REF, _authority
from tests.gateway.test_update_requeue_stop_race import _stop_session, _until, _yielded


async def _requeue_racing(tmp_path, monkeypatch, *, during_write, payload_extra=None):
    """Admit one notifier wake that yields unstarted as the hold starts, run ``during_write(kwargs)`` on the
    requeue write's thread before the write itself, and return what the test needs to check the outcome."""
    import hermes_state_update_pause
    from gateway import session_finite, session_notifier_drain
    from hermes_state_runtime import admit_session_input
    from tests.hermes_state.test_notifier_claim_eligibility import wake_payload
    db, authority = _authority(tmp_path, monkeypatch)
    held, calls = [False], []
    monkeypatch.setattr(session_notifier_drain, 'held', lambda: held[0])
    original = hermes_state_update_pause.requeue_unstarted_input

    def racing_write(*args, **kwargs):
        during_write(authority, kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(hermes_state_update_pause, 'requeue_unstarted_input', racing_write)

    async def execute(owner, ref, row):
        calls.append(row)
        held[0] = True
        _yielded(owner, row)
        return 'answered'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)
    identity = json.dumps(['kanban-wake', 'route', 'latch'])
    payload = {**wake_payload(identity, local=True), **(payload_extra or {})}
    head = admit_session_input(db, epoch=authority.epoch, session_id='s', principal_id='automation:route',
                               request_id=identity, payload=payload)
    waiter = authority.waiters.setdefault(head['admission_id'], asyncio.get_running_loop().create_future())
    return db, authority, head, waiter, held, calls


async def _cancelled_and_not_rerun(db, authority, head, waiter, held, calls):
    from gateway.session_runtime_workers import mutation_tasks
    from hermes_state_runtime import get_session_admission
    await _until(lambda: calls and not mutation_tasks(authority))
    row = get_session_admission(db, admission_id=head['admission_id'])
    assert (row['status'], row['outcome']) == ('terminal', 'cancelled'), row
    assert await asyncio.wait_for(waiter, 5) is None
    held[0] = False
    authority.sessions['s'].claim_changed.set()
    await _until(lambda: not mutation_tasks(authority))
    assert len(calls) == 1, 'the stopped notice ran after the hold'


@pytest.mark.asyncio
async def test_a_shutdown_stop_latched_during_the_requeue_write_is_consumed_with_the_head(tmp_path, monkeypatch):
    """A shutdown Stop arriving while the requeue write runs finds the turn returned with no adopted agent and
    latches its generation in ``pending_stops``. The head is cancelled, and that latch must go with it: left
    behind, it names a settled generation and the session's next adoption of it would be stopped."""
    from gateway.run_runtime import stop_authority_turns
    signalled = []

    def shutdown_stop(authority, kwargs):
        signalled.append(stop_authority_turns(authority, in_process=True, reason='gateway shutdown'))
        assert authority.pending_stops.get('s') == kwargs['generation'], 'the shutdown Stop never latched'

    db, authority, head, waiter, held, calls = await _requeue_racing(tmp_path, monkeypatch,
                                                                    during_write=shutdown_stop)
    with db:
        authority._schedule(REF)
        try:
            await _cancelled_and_not_rerun(db, authority, head, waiter, held, calls)
            assert signalled == [1]
            assert 's' not in authority.pending_stops, authority.pending_stops
            assert 's' not in authority.pending_stop_causes
            assert head['admission_id'] not in authority.update_stops
        finally:
            held[0] = False
            await _stop_session(authority)


@pytest.mark.asyncio
async def test_the_stopped_heads_retained_media_is_released_when_it_is_cancelled(tmp_path, monkeypatch):
    """A Stop latched during the requeue write cancels the head; its retained native media has no other owner
    and must be released with it, not left on disk until the 24 h settled-media sweep."""
    from pathlib import Path

    from gateway.session_ingress_media import capture_native_media
    from gateway.session_update_pause import interrupt_update
    from tests.gateway.test_native_media_ownership import _staged
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))  # media store under tmp_path before capture
    media = capture_native_media([_staged('photo.png', 64)])
    retained = Path(media[0]['path'])
    assert retained.exists()
    loop = asyncio.get_running_loop()
    stops = []

    def user_stop(authority, kwargs):
        stops.append(asyncio.run_coroutine_threadsafe(interrupt_update(authority, REF, kwargs['generation']), loop))
        from tests.waiting import wait_until
        wait_until(lambda: kwargs['admission_id'] in authority.update_stops, timeout=5)

    db, authority, head, waiter, held, calls = await _requeue_racing(
        tmp_path, monkeypatch, during_write=user_stop,
        payload_extra={'attachments_v1': {'media': media, 'media_types': ['image/png']}})
    with db:
        authority._schedule(REF)
        try:
            await _until(lambda: stops and stops[0].done())
            assert stops[0].result() is True
            await _cancelled_and_not_rerun(db, authority, head, waiter, held, calls)
            assert not retained.exists(), 'the cancelled head kept its media'
        finally:
            held[0] = False
            await _stop_session(authority)
