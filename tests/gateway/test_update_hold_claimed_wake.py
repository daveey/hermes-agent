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


def _turn_start(agent_history, event, agent=None):
    """What TurnRunner does for the agent run: install the hook for the bound scope, then the agent's own
    turn-start check (``conversation_loop`` runs it after acquiring the session turn lease)."""
    from agent.update_pause_cancel import pause_before_turn
    from gateway.run_turn_runner import project_paused_result
    from gateway.session_update_pause_hook import installed
    agent = SimpleNamespace() if agent is None else agent
    with installed(agent, message_id=event.message_id):
        paused = pause_before_turn(agent, agent_history)
    return None if paused is None else project_paused_result(paused, {})


@pytest.mark.asyncio
@pytest.mark.parametrize('checkpointable', [True, False], ids=['supported-scope', 'unsupported-scope'])
async def test_wake_claimed_before_the_hold_goes_back_to_its_fifo_until_the_hold_lifts(tmp_path, monkeypatch,
                                                                                         checkpointable):
    """[daveey] t_01a1d4d4c30c7e43 (unsupported-scope): a wake whose scope cannot checkpoint (``supported`` False:
    a detached route's wake) re-queues the same way. Its turn would otherwise run unpausable inside the window,
    and the reclaim preflight refuses such a chat unit as ``unmatched gateway work``."""
    from gateway import session_notifier_drain, session_update_pause, session_update_pause_ingress
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
    if not checkpointable:
        monkeypatch.setattr(session_update_pause_ingress, 'supported', lambda *a, **k: False)
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


def test_detached_route_wake_gets_only_the_turn_start_gate(tmp_path, monkeypatch):
    """[daveey] t_01a1d4d4c30c7e43: the real ``supported`` refuses a detached route's ``local_automation_v1`` wake (its
    resume is not covered), so no boundary hook is installed and it never reads ``update_pausable``; its turn
    still yields before it starts while the claim would hold it."""
    import json
    from agent.update_pause_cancel import BEFORE_TURN_ATTR
    from gateway import session_notifier_drain
    from gateway.session_update_pause_hook import _HOOK_ATTR, installed, update_pausable
    from gateway.session_update_pause_ingress import pause_scope, supported
    from tui_gateway.update_pause import PAUSE_REASON
    held = [True]
    monkeypatch.setattr(session_notifier_drain, 'held', lambda: held[0])
    monkeypatch.setattr('gateway.session_tui_ops._profile_home', lambda authority: tmp_path)
    identity = json.dumps(['kanban-wake', 'route', 'x'], separators=(',', ':'))
    wake = {'payload': {'text': 'wake', 'local_automation_v1': {'identity': identity, 'owner': 's'}},
            'request_id': identity, 'principal_id': 'automation:route'}
    live = SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value='telegram')))
    assert supported(None, wake, None, live, is_api=False) is False
    with pause_scope(SimpleNamespace(), wake, SimpleNamespace(message_id='m1'), live, is_api=False):
        agent = SimpleNamespace()
        with installed(agent, message_id='m1') as hooked:
            assert hooked is False and not update_pausable(agent) and _HOOK_ATTR not in vars(agent)
            gate = vars(agent)[BEFORE_TURN_ATTR]
            assert gate() == PAUSE_REASON
            held[0] = False
            assert gate() is None
        assert BEFORE_TURN_ATTR not in vars(agent)  # removed after the run (the agent is cached)


async def _wake_session(tmp_path, monkeypatch, instance_id):
    """A composed local Desktop-style chat (interactive ``tui`` source) whose Kanban wake the test admits; the
    notifier hold is ``held[0]``."""
    from gateway import session_notifier_drain, session_update_pause
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    from hermes_cli import admission_fence
    from hermes_state import SessionDB

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id='default', instance_id=instance_id, db=db)
    held = [False]
    monkeypatch.setattr(session_notifier_drain, 'held', lambda: held[0])
    monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: held[0])
    monkeypatch.setattr(admission_fence, 'pause_requested', lambda root: held[0])
    connection = AuthorityConnection(authority, object(), {'user_id': 'owner'})
    ref = create_local_session(authority, connection.actor, dict(request_id='cli', source='tui', model='fixture',
                                                                 cwd=str(tmp_path), toolsets=[]))
    await authority.attach(connection.actor, ref, history=False)
    connection.subscriptions[ref.session_id] = 'attached'
    return runner, db, authority, ref, connection, held


async def _close(authority, db, held, connection):
    held[0] = False
    await connection.close()
    tasks = [live.task for live in authority.sessions.values() if live.task]
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    db.close()


@pytest.mark.asyncio
async def test_a_steer_committed_while_the_wake_waited_on_the_lease_goes_back_with_it(tmp_path, monkeypatch):
    """[daveey] t_01a1d4d4c30c7e43: a queued prompt steered into the claimed wake after its turn-start check (the turn
    was still waiting on the session turn lease) used to make ``requeue_unstarted_input`` refuse ("steering
    transfer exists"), then the checkpoint refused too and the wake settled interrupted: lost. Now the transfer the
    turn never took goes back to its own FIFO place in the requeue's write, and both still run once."""
    from gateway.session_contract import Submission
    from gateway.session_automation import admit_route_wake
    from hermes_state_runtime import list_session_admissions
    from tests.gateway.test_prompt_queued_transfer import BufferedAgent
    from tests.waiting import async_wait_until

    runner, db, authority, ref, connection, held = await _wake_session(tmp_path, monkeypatch, 'wake-steer')
    sid = ref.session_id
    wake, ran, steer_replies = '[IMPORTANT: Kanban task updates arrived]', [], []
    agent = BufferedAgent()

    async def turn(event, source, quick_key, run_generation):
        if event.text != wake or steer_replies:
            assert event.text != wake or not held[0], 'a held wake was claimed again inside the hold'
            ran.append(event.text)
            execution_result.get()['result'] = {'final_response': 'done', 'messages': []}
            return 'done'
        authority.adopt_agent(sid, authority.executing[sid], agent)
        held[0] = True  # the hold came on while the turn waited on the session turn lease
        projected = _turn_start([], event, agent)
        assert projected is not None, 'the wake must yield at turn start'
        # The user steers the prompt queued behind the wake into it before the yielded turn returned.
        follow = await authority.submit(connection.actor, Submission('follow-up', ref, {'text': 'follow-up'}, 'queue'))
        steer_replies.append((follow.admission_id, await connection.dispatch({
            'id': 1, 'method': 'prompt.steer_queued',
            'params': {'session_id': sid, 'admission_id': follow.admission_id}})))
        execution_result.get()['result'] = projected
        return None

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    try:
        live = authority.sessions[sid]
        adapter = runner._adapter_for_source(live.source)
        assert await admit_route_wake(runner, adapter, live.source, wake, 'kanban-wake') is True
        await async_wait_until(lambda: ran == ['follow-up'], timeout=10, what='the handed-back prompt ran')
        follow_id, reply = steer_replies[0]
        assert 'error' not in reply, reply  # the steer itself was accepted into the claim
        rows = {r['admission_id']: r for r in list_session_admissions(db, session_id=sid, pending_only=False)}
        wake_row = next(r for r in rows.values() if r['admission_id'] != follow_id)
        assert (wake_row['status'], wake_row['outcome']) == ('queued', None), 'the wake was lost'
        assert agent._pending_steer is None  # withdrawn: never left buffered for a later turn
        held[0] = False
        authority._schedule(ref)
        await async_wait_until(lambda: ran == ['follow-up', wake], timeout=10, what='the wake ran after the lift')
        await asyncio.wait_for(authority.sessions[sid].task, 10)
        rows = list_session_admissions(db, session_id=sid, pending_only=False)
        assert sorted((r['admission_id'] == follow_id, r['status'], r['outcome']) for r in rows) == [
            (False, 'terminal', 'completed'), (True, 'terminal', 'completed')]
    finally:
        await _close(authority, db, held, connection)


@pytest.mark.asyncio
async def test_real_turn_runner_and_agent_requeue_the_unstarted_wake_then_run_it_once(tmp_path, monkeypatch):
    """[daveey] t_01a1d4d4c30c7e43: end to end through the real TurnRunner -> AIAgent.run_conversation ->
    pause_before_turn -> project_paused_result -> retain_pause (pending_results) -> park_or_interrupt wiring. Only
    agent allocation and the provider are controlled; the hold comes on after the claim, before the turn starts."""
    import copy
    from gateway.run_turn_runner import TurnRunner
    from gateway.session_automation import admit_route_wake
    from hermes_state_runtime import list_session_admissions
    from tests.agent.test_step_boundary_pause import _final_response, _make_agent
    from tests.waiting import async_wait_until

    runner, db, authority, ref, connection, held = await _wake_session(tmp_path, monkeypatch, 'wake-e2e')
    physical = authority.physical_target(ref)
    agent = _make_agent([])
    del agent._execute_tool_calls
    agent.session_id, agent._session_db = physical, db
    agent._save_trajectory = lambda *a, **k: None
    agent._cleanup_task_resources = lambda *a, **k: None
    provider_inputs, allocations = [], []

    def provider(**kwargs):
        provider_inputs.append(copy.deepcopy(kwargs['messages']))
        return _final_response()

    agent.client.chat.completions.create.side_effect = provider

    def allocate(turn, *a, **k):
        allocations.append(turn._ctx.message)
        if len(allocations) == 1:
            held[0] = True  # the hold came on between the claim and the turn's start (the lease wait)
        turn._ctx.agent_holder[0] = agent
        return agent, len(allocations) > 1

    monkeypatch.setattr(TurnRunner, '_resolve_turn_agent', allocate)
    monkeypatch.setattr(runner, '_resolve_session_agent_runtime', lambda **k: ('test/model', {}))
    wake = '[IMPORTANT: 28 Kanban task updates arrived while this session was busy]'
    try:
        live = authority.sessions[ref.session_id]
        adapter = runner._adapter_for_source(live.source)
        assert await admit_route_wake(runner, adapter, live.source, wake, 'kanban-wake') is True

        def requeued():
            rows = list_session_admissions(db, session_id=ref.session_id)
            return ([(r['status'], r['outcome']) for r in rows] == [('queued', None)] and not authority.pending_results
                    and authority.sessions[ref.session_id].event_stream.execution == {})
        await async_wait_until(lambda: len(allocations) == 1 and requeued(), timeout=15,
                               what='the unstarted wake went back to its FIFO place')
        assert provider_inputs == [], 'the wake sent a model request inside the update hold'
        assert db.get_messages(physical) == [], 'the unstarted wake wrote its transcript'
        assert not authority.update_parked and not authority.executing
        first_id = list_session_admissions(db, session_id=ref.session_id)[0]['admission_id']

        held[0] = False
        authority._schedule(ref)
        await async_wait_until(lambda: len(provider_inputs) == 1, timeout=15, what='the wake ran after the lift')
        await asyncio.wait_for(authority.sessions[ref.session_id].task, 15)
        rows = list_session_admissions(db, session_id=ref.session_id, pending_only=False)
        assert [(r['admission_id'], r['status'], r['outcome']) for r in rows] == [(first_id, 'terminal', 'completed')]
        assert len(allocations) == 2 and provider_inputs[0][-1]['content'] == wake
        stored = db.get_messages(physical)
        assert [m['role'] for m in stored] == ['user', 'assistant'] and stored[-1]['content'] == 'done'
    finally:
        await _close(authority, db, held, connection)
