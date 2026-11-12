"""Local canonical resume keeps a typed notice without granting internal ingress authority."""
import asyncio
import pytest
from gateway.session_results import execution_result


@pytest.mark.asyncio
async def test_local_checkpoint_resumes_as_notice_on_exact_physical_session(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_contract import Submission
    from gateway.session_local import create_local_session
    from gateway.response_filters import display_kind_for_event
    from gateway import session_update_pause
    from tests.waiting import async_wait_until
    from tui_gateway.update_pause import pause_note

    runner = GatewayRunner(GatewayConfig())
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='local-pause')
    connection = AuthorityConnection(authority, object(), {'user_id': 'owner'})
    ref = create_local_session(authority, connection.actor, {'request_id': 'local', 'source': 'tui',
        'cwd': str(tmp_path), 'model': 'm', 'toolsets': []})
    # Keep the conversation viewed, so the local completion-note path does not consume it.
    await authority.attach(connection.actor, ref, history=False)
    held, seen = [True], []
    monkeypatch.setattr(session_update_pause, 'update_held', lambda authority: held[0])

    async def turn(event, source, quick_key, run_generation):
        seen.append(event)
        if len(seen) == 1:
            authority.db.append_messages_batch(ref.session_id, [
                {'role': 'user', 'content': 'original'},
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'a', 'type': 'function', 'function': {'name': 'example', 'arguments': '{}'}}]},
                {'role': 'tool', 'content': 'done', 'tool_call_id': 'a', 'tool_name': 'example'}])
            execution_result.get()['result'] = {'paused_for_update': True}
            return None
        assert event.internal is False
        assert event.text == pause_note('original')
        assert display_kind_for_event(event) == 'auto_continue'
        resolved = await runner._hmwa_resolve_session(event, source)
        assert resolved[1].session_id == ref.session_id
        execution_result.get()['result'] = {'final_response': 'continued', 'messages': []}
        return 'continued'
    monkeypatch.setattr(runner, '_handle_message_with_agent', turn)
    try:
        receipt = await authority.submit(connection.actor, Submission('original', ref, {'text': 'original'}, 'queue'))
        await async_wait_until(lambda: bool(authority.update_parked), timeout=5)
        assert len(seen) == 1
        held[0] = False
        await asyncio.wait_for(authority.sessions[ref.session_id].task, 5)
        assert len(seen) == 2
        from hermes_state_runtime import get_session_admission
        assert get_session_admission(authority.db, admission_id=receipt.admission_id)['outcome'] == 'completed'
    finally:
        held[0] = False
        tasks = [live.task for live in authority.sessions.values() if live.task]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await connection.close()
        authority.db.close()
