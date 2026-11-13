"""Every notice pending at a turn boundary rides the turn that starts there (t_01a1d663082aa53a).

A completion admitted mid-turn waited one full turn per row queued ahead of it: an update-resume
continuation and a Kanban batch each started without it, 59 min in all on the MacBook. The turn that
starts now carries every completion and Kanban wake already queued for the session, in admission order,
with each row's own text intact; each row is acked once by the claim's settlement, and an ending other
than completed (failed, paused for an update) gives every folded row back to its FIFO place.
"""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.session_results import execution_result


async def _harness(tmp_path, monkeypatch, behave):
    from gateway import session_update_pause
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import SessionActor
    from gateway.session_local import create_local_session
    from hermes_cli import admission_fence
    from hermes_state import SessionDB

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / 'sessions'))
    db = SessionDB(tmp_path / 'state.db')
    runner.session_store._db = runner._session_db = db
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='fold', db=db)
    held = [False]
    monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: held[0])
    monkeypatch.setattr(admission_fence, 'pause_requested', lambda root: held[0])
    seen = []

    async def turn(event, source, quick_key, run_generation):
        seen.append(event)
        return await behave(len(seen), event)

    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    viewer = SessionActor('test-owner', 'default', frozenset(
        {'session:create', 'session:submit', 'session:read'}), 'viewer')
    ref = create_local_session(authority, viewer, dict(request_id='cli', source='tui', model='fixture',
                                                      cwd=str(tmp_path), toolsets=[]))
    await authority.attach(viewer, ref, history=False)  # viewed: completions run as turns, not notes
    live = authority.sessions[ref.session_id]
    adapter = runner._adapter_for_source(live.source)

    async def notice(text, producer):
        from gateway.session_automation import admit_route_wake
        assert await admit_route_wake(runner, adapter, live.source, text, producer) is True

    def rows():
        from hermes_state_runtime import list_session_admissions
        return {row['payload']['text']: row
                for row in list_session_admissions(db, session_id=ref.session_id, pending_only=False)}

    async def close():
        held[0] = False
        tasks = [entry.task for entry in authority.sessions.values() if entry.task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        db.close()

    return SimpleNamespace(authority=authority, ref=ref, live=live, held=held, seen=seen, notice=notice,
                           rows=rows, close=close, db=db, viewer=viewer)


def _completed(text='done'):
    execution_result.get()['result'] = {'final_response': text, 'messages': []}
    return text


async def _idle(h, turns):
    from tests.waiting import async_wait_until
    await async_wait_until(lambda: len(h.seen) >= turns, timeout=10, what=f'{turns} turns')
    await asyncio.wait_for(h.live.task, 10)


def _pause(event):
    """A turn that reached a completed-tool step boundary and paused for a Hermes update."""
    from gateway.session_update_pause_hook import installed
    from agent.turn_iteration_prep import _step_boundary_pause
    sid = event.metadata['gateway_session_id']
    history = [{'role': 'user', 'content': event.text},
               {'role': 'assistant', 'content': '', 'tool_calls': [
                   {'id': 'k1', 'type': 'function', 'function': {'name': 'kanban_show', 'arguments': '{}'}}]},
               {'role': 'tool', 'content': 'card', 'tool_call_id': 'k1', 'tool_name': 'kanban_show'}]
    agent = SimpleNamespace()
    with installed(agent, message_id=event.message_id):
        assert _step_boundary_pause(agent, history, 1)
    execution_result.get()['result'] = {'paused_for_update': True, 'interrupted': True,
                                        'final_response': '', 'messages': history}
    return sid, history


@pytest.mark.asyncio
async def test_update_resume_turn_carries_a_completion_admitted_while_paused(tmp_path, monkeypatch):
    """(a) The completion admitted while the turn was paused rides the resume turn, after the paused request."""
    from tests.waiting import async_wait_until
    from tui_gateway.update_pause import pause_note
    h = None

    async def behave(n, event):
        if n == 1:
            sid, history = _pause(event)
            h.authority.db.append_messages_batch(sid, history)
            return None
        return _completed()

    h = await _harness(tmp_path, monkeypatch, behave)
    try:
        h.held[0] = True
        await h.notice('[kanban] Task t_1 blocked.', 'kanban-wake')
        await async_wait_until(lambda: bool(h.authority.update_parked), timeout=10, what='the wake parked')
        await h.notice('[IMPORTANT: Background process proc_a completed (exit 0)]', 'completion')
        h.held[0] = False
        h.authority._schedule(h.ref)
        await _idle(h, 2)
        assert len(h.seen) == 2, [event.text for event in h.seen]
        assert h.seen[1].text == (pause_note('[kanban] Task t_1 blocked.')
                                  + '\n\n[IMPORTANT: Background process proc_a completed (exit 0)]')
        rows = h.rows()
        assert (rows['[kanban] Task t_1 blocked.']['outcome']) == 'completed'
        assert rows['[IMPORTANT: Background process proc_a completed (exit 0)]']['outcome'] == 'steered'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_kanban_row_then_completion_run_as_one_turn_in_admission_order(tmp_path, monkeypatch):
    """(b) Both queued behind a busy turn: the next boundary starts ONE turn carrying both, in order."""
    gate = asyncio.Event()

    async def behave(n, event):
        if n == 1:
            await gate.wait()
        return _completed()

    h = await _harness(tmp_path, monkeypatch, behave)
    try:
        await h.notice('[kanban] Task t_0 completed.', 'kanban-wake')
        from tests.waiting import async_wait_until
        await async_wait_until(lambda: len(h.seen) == 1, timeout=10, what='the first turn')
        await h.notice('[kanban] Task t_1 blocked.', 'kanban-wake')
        await h.notice('[IMPORTANT: Background process proc_a completed (exit 0)]', 'completion')
        gate.set()
        await _idle(h, 2)
        assert [event.text for event in h.seen] == [
            '[kanban] Task t_0 completed.',
            '[kanban] Task t_1 blocked.\n\n[IMPORTANT: Background process proc_a completed (exit 0)]']
        rows = h.rows()
        assert rows['[kanban] Task t_1 blocked.']['outcome'] == 'completed'
        assert rows['[IMPORTANT: Background process proc_a completed (exit 0)]']['outcome'] == 'steered'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['completed', 'failed', 'paused'])
async def test_two_completions_and_a_kanban_row_fold_and_settle_once(tmp_path, monkeypatch, ending):
    """(c) One turn carries all three. Each is acked exactly once; a failed or paused turn gives every
    folded row back to its FIFO place, and the next turn carries them (no loss, no duplicate)."""
    from tui_gateway.update_pause import pause_note
    gate = asyncio.Event()
    c1, k, c2 = ('[IMPORTANT: Background process proc_a completed (exit 0)]', '[kanban] Task t_1 blocked.',
                 '[IMPORTANT: Background process proc_b completed (exit 1)]')
    h = None

    async def behave(n, event):
        if n == 1:
            await gate.wait()
            return _completed()
        if n == 2 and ending == 'failed':
            execution_result.get()['result'] = {'final_response': '', 'messages': [], 'failed': True,
                                                'error': 'provider down'}
            return ''
        if n == 2 and ending == 'paused':
            h.held[0] = True
            sid, history = _pause(event)
            h.authority.db.append_messages_batch(sid, history)
            return None
        return _completed()

    h = await _harness(tmp_path, monkeypatch, behave)
    try:
        from tests.waiting import async_wait_until
        await h.notice('[kanban] Task t_0 completed.', 'kanban-wake')
        await async_wait_until(lambda: len(h.seen) == 1, timeout=10, what='the first turn')
        # A paused turn needs a pause-eligible head: a Kanban wake (a local completion turn never parks).
        order = (k, c1, c2) if ending == 'paused' else (c1, k, c2)
        for text in order:
            await h.notice(text, 'kanban-wake' if text == k else 'completion')
        gate.set()
        if ending == 'paused':
            await async_wait_until(lambda: bool(h.authority.update_parked), timeout=10, what='the turn parked')
            rows = h.rows()
            # The parked head keeps its checkpoint; the rows it carried are queued again in their places.
            assert [rows[text]['status'] for text in order] == ['started', 'queued', 'queued']
            h.held[0] = False
            h.authority._schedule(h.ref)
        await _idle(h, 2 if ending == 'completed' else 3)
        texts = [event.text for event in h.seen]
        assert texts[1] == '\n\n'.join(order)
        rows = h.rows()
        if ending == 'completed':
            assert len(texts) == 2
            assert [rows[text]['outcome'] for text in (c1, k, c2)] == ['completed', 'steered', 'steered']
        elif ending == 'failed':
            assert len(texts) == 3 and texts[2] == f'{k}\n\n{c2}'
            assert [rows[text]['outcome'] for text in (c1, k, c2)] == ['failed', 'completed', 'steered']
        else:
            assert len(texts) == 3 and texts[2] == pause_note(k) + f'\n\n{c1}\n\n{c2}'
            assert [rows[text]['outcome'] for text in order] == ['completed', 'steered', 'steered']
        assert not [row for row in rows.values() if row['status'] != 'terminal']
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_a_queued_prompt_is_never_jumped_and_never_extended(tmp_path, monkeypatch):
    """A notice queued behind a person's message waits for it; the message itself stays as typed."""
    from gateway.session_contract import Submission
    gate = asyncio.Event()

    async def behave(n, event):
        if n == 1:
            await gate.wait()
        return _completed()

    h = await _harness(tmp_path, monkeypatch, behave)
    try:
        from tests.waiting import async_wait_until
        await h.notice('[kanban] Task t_0 completed.', 'kanban-wake')
        await async_wait_until(lambda: len(h.seen) == 1, timeout=10, what='the first turn')
        await h.notice('[kanban] Task t_1 blocked.', 'kanban-wake')
        await h.authority.submit(h.viewer, Submission('my question', h.ref, {'text': 'my question'}, 'queue'))
        await h.notice('[IMPORTANT: Background process proc_a completed (exit 0)]', 'completion')
        gate.set()
        await _idle(h, 4)
        assert [event.text for event in h.seen][1:] == [
            '[kanban] Task t_1 blocked.', 'my question', '[IMPORTANT: Background process proc_a completed (exit 0)]']
    finally:
        await h.close()
