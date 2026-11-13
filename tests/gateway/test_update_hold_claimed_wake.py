"""[daveey] t_01a1d49d50e4c656: a notifier wake claimed before a core-update hold never starts inside it.

David-MBP incident 31763 (2026-11-13 PST): the gateway claimed a folded Kanban-wake batch for the Desktop chat
20261026_235722_bef6c6 at 02:54, before the hold. Its turn then waited on the session turn lease (a Desktop
backend turn held it) until 03:07:12, when that turn parked for the update; the waiting wake started at once,
inside the drain window, and kept the reclaim from finding the chat idle. A claim is checked against the hold
once; the turn it hands off may begin much later.

Contract: a queued notifier wake waits in its FIFO while the hold is on (``session_notifier_drain``). A claimed
one whose turn has not begun when the hold comes on yields before its turn starts and goes back to its FIFO
place, queued, unsettled and unrun; it runs once the hold lifts. A human or a resumed checkpoint is untouched.
"""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.session_results import execution_result


def _turn_start(agent_history, event):
    """What TurnRunner does for the agent run: install the hook for the bound scope, then the agent's own
    turn-start check (``conversation_loop`` runs it after acquiring the session turn lease)."""
    from agent.update_pause_cancel import pause_before_turn
    from gateway.run_turn_runner import project_paused_result
    from gateway.session_update_pause_hook import installed
    agent = SimpleNamespace()
    with installed(agent, message_id=event.message_id):
        paused = pause_before_turn(agent, agent_history)
    return None if paused is None else project_paused_result(paused, {})


@pytest.mark.asyncio
async def test_wake_claimed_before_the_hold_goes_back_to_its_fifo_until_the_hold_lifts(tmp_path, monkeypatch):
    from gateway import session_notifier_drain, session_update_pause
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_automation import admit_route_wake
    from gateway.session_contract import SessionActor
    from gateway.session_local import create_local_session
    from hermes_cli import admission_fence
    from hermes_state import SessionDB
    from hermes_state_runtime import list_session_admissions
    from tests.waiting import async_wait_until

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='wake-hold', db=db)
    held, asked = [False], []
    monkeypatch.setattr(session_notifier_drain, 'held', lambda: asked.append(1) or held[0])
    monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: held[0])
    monkeypatch.setattr(admission_fence, 'pause_requested', lambda root: held[0])
    started, ran = [], []

    async def turn(event, source, quick_key, run_generation):
        started.append(event.text)
        # The turn waited on the session turn lease (another backend's turn held it) across the start
        # of the hold: the hold is on by the time this turn would begin.
        held[0] = True
        projected = _turn_start([], event)
        if projected is not None:
            execution_result.get()['result'] = projected
            return None
        ran.append(event.text)
        execution_result.get()['result'] = {'final_response': 'done', 'messages': []}
        return 'done'

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    try:
        viewer = SessionActor('test-owner', 'default', frozenset(
            {'session:create', 'session:submit', 'session:read'}), 'viewer')
        ref = create_local_session(authority, viewer, dict(request_id='cli', source='tui', model='fixture',
                                                          cwd=str(tmp_path), toolsets=[]))
        await authority.attach(viewer, ref, history=False)
        live = authority.sessions[ref.session_id]
        adapter = runner._adapter_for_source(live.source)
        wake = '[IMPORTANT: 28 Kanban task updates arrived while this session was busy]'
        assert await admit_route_wake(runner, adapter, live.source, wake, 'kanban-wake') is True
        await async_wait_until(lambda: len(started) == 1, timeout=10, what='the wake was claimed')

        def requeued():
            rows = list_session_admissions(db, session_id=ref.session_id)
            return [(r['status'], r['outcome']) for r in rows] == [('queued', None)] \
                and not authority.pending_results and authority.sessions[ref.session_id].event_stream.execution == {}
        await async_wait_until(lambda: requeued() or bool(ran), timeout=10, what='the wake settled its first claim')
        assert ran == [], 'the wake began its turn inside the update hold'
        assert requeued() and not authority.update_parked
        assert not authority.executing

        # The drain keeps it queued behind the held claim (several claim polls), never re-claiming it.
        polls = len(asked)
        authority._schedule(ref)
        await async_wait_until(lambda: len(asked) >= polls + 3, timeout=10, what='the drain re-read the hold')
        assert len(started) == 1 and requeued()
        first_id = list_session_admissions(db, session_id=ref.session_id)[0]['admission_id']

        async def later(event, source, quick_key, run_generation):
            started.append(event.text)
            assert _turn_start([], event) is None
            ran.append(event.text)
            execution_result.get()['result'] = {'final_response': 'done', 'messages': []}
            return 'done'
        monkeypatch.setattr(runner, '_handle_message_with_agent', later)
        held[0] = False  # the hold lifts
        authority._schedule(ref)
        await async_wait_until(lambda: ran == [wake], timeout=10, what='the wake ran after the hold lifted')
        await asyncio.wait_for(authority.sessions[ref.session_id].task, 10)
        rows = list_session_admissions(db, session_id=ref.session_id, pending_only=False)
        assert [(r['admission_id'], r['status'], r['outcome']) for r in rows] == [
            (first_id, 'terminal', 'completed')]  # the same admission, run once
    finally:
        held[0] = False
        tasks = [live.task for live in authority.sessions.values() if live.task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        db.close()


@pytest.mark.asyncio
async def test_human_turn_claimed_before_the_hold_still_runs(tmp_path, monkeypatch):
    """Humans keep their admission semantics under the claim hold: only the held class yields."""
    from gateway import session_notifier_drain, session_update_pause
    from gateway.session_update_pause_ingress import pause_scope
    from gateway.session_update_pause_hook import installed
    from agent.update_pause_cancel import BEFORE_TURN_ATTR
    monkeypatch.setattr(session_notifier_drain, 'held', lambda: True)
    monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: True)
    authority = SimpleNamespace(profile_id=str(tmp_path))
    monkeypatch.setattr('gateway.session_tui_ops._profile_home', lambda authority: tmp_path)
    live = SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value='local')))
    event = SimpleNamespace(message_id='m1')
    human = {'payload': {'text': 'hi'}, 'request_id': 'r', 'principal_id': 'uid:501'}
    with pause_scope(authority, human, event, live, is_api=False):
        agent = SimpleNamespace()
        with installed(agent, message_id='m1') as hooked:
            assert hooked
            assert BEFORE_TURN_ATTR not in vars(agent)
