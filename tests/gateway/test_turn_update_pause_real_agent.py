"""Real ``AIAgent`` (controlled model client, real tool-batch boundary) behind the real ``TurnRunner`` seam.

``test_turn_update_pause`` stubs the agent; this composes the production hook installation, the
admission fence, ``_step_boundary_pause`` and ``conversation_loop``'s paused result for a LOCAL chat turn.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner, project_paused_result
from gateway.session import SessionSource
from gateway.session_update_pause_hook import bind_scope, is_paused
from gateway.turn_context import TurnContext
from hermes_cli import admission_fence
from tests.agent.test_step_boundary_pause import _final_response, _make_agent, _tool_response
from tui_gateway.update_pause import PAUSE_REASON


def _local_turn():
    return TurnRunner(
        SimpleNamespace(session_authority=None, _consume_pending_native_image_paths=lambda key: []),
        TurnContext(message='do some tool work', session_id='chat', session_key='agent:main:local:dm:chat',
                    source=SessionSource(platform=Platform.LOCAL, chat_id='chat', user_id='u')))


def _drive(tmp_path, *, request, eligible=True, stop=False):
    """Run a real agent turn through the runner; the request lands while its first tool runs."""
    events: list = []
    tool_started = threading.Event()
    release_tool = threading.Event()

    def tool_start():
        tool_started.set()
        assert release_tool.wait(10), 'pause/stop publication did not release the tool'

    agent = _make_agent(events, on_tool_start=tool_start)
    agent.client.chat.completions.create.side_effect = [_tool_response(1), _final_response()]
    turn = _local_turn()
    from hermes_state import SessionDB
    import hermes_state_runtime as rt
    agent.session_id = 'chat'
    agent._session_db = SessionDB(tmp_path / 'state.db')
    agent._session_db.create_session('chat', source='local')
    epoch = rt.begin_runtime_epoch(agent._session_db, instance_id='owner')
    rt.admit_session_input(agent._session_db, epoch=epoch, principal_id='human', session_id='chat',
                           request_id='original', payload={'text': 'do some tool work'})
    agent._test_claim = rt.claim_session_input(agent._session_db, epoch=epoch, session_id='chat')
    agent._test_epoch = epoch

    def go():
        with bind_scope(tmp_path, eligible=eligible), \
                patch.object(agent, '_save_trajectory'), \
                patch.object(agent, '_cleanup_task_resources'):
            return turn._run_conversation_with_approval(agent, [], None, None, None)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(go)
            try:
                assert tool_started.wait(10)
                if stop:
                    agent.interrupt()
                if request:
                    admission_fence.set_hold(tmp_path)
                    admission_fence.request_pause(tmp_path, expires_at=time.time() + 60)
            finally:
                release_tool.set()
            return agent, running.result(timeout=20), events
    finally:
        admission_fence.clear_hold(tmp_path)
        admission_fence.clear_pause_request(tmp_path)


def test_real_agent_local_turn_parks_after_tool_without_next_model_call(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent, result, events = _drive(tmp_path, request=True)
    assert result['paused_for_update'] == PAUSE_REASON and result['interrupted'] is True
    assert agent.client.chat.completions.create.call_count == 1  # no model call after the boundary
    from hermes_state_update_pause import checkpoint_session_input, get_session_checkpoint
    row = agent._test_claim
    stored = agent._session_db.get_messages('chat')
    assert [m['role'] for m in stored] == ['user', 'assistant', 'tool'], [(m['role'], m['content']) for m in stored]
    receipt = checkpoint_session_input(agent._session_db, epoch=agent._test_epoch,
        admission_id=row['admission_id'], generation=row['generation'], physical_session_id='chat',
        transcript_mark=row['transcript_mark'])
    assert get_session_checkpoint(agent._session_db, session_id='chat') == receipt
    roles = [m['role'] for m in result['messages']]
    assert roles == ['user', 'assistant', 'tool']
    assert 'ok' == result['messages'][2]['content']
    assert ('tool_end', 'call_1') in events
    assert not callable(vars(agent).get('step_boundary_pause'))  # hook removed after the run
    assert is_paused(result)  # the REAL agent stamps the reason string, not True (stubs used True)
    assert project_paused_result(result, {'messages': result['messages'], 'error': None, 'interrupted': True}
                                 )['final_response'] == ''
    agent._session_db.close()


def test_real_agent_local_turn_without_request_completes(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent, result, _ = _drive(tmp_path, request=False)
    assert 'paused_for_update' not in result and result['final_response'] == 'done'
    assert agent.client.chat.completions.create.call_count == 2
    agent._session_db.close()


def test_real_agent_ineligible_turn_is_never_parked_even_with_request(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent, result, _ = _drive(tmp_path, request=True, eligible=False)
    assert 'paused_for_update' not in result and result['final_response'] == 'done'
    assert agent.client.chat.completions.create.call_count == 2
    agent._session_db.close()


def test_real_user_stop_still_closes_durable_tool_tail(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent, result, _ = _drive(tmp_path, request=True, stop=True)
    assert result['interrupted'] is True and not is_paused(result)
    assert agent.client.chat.completions.create.call_count == 1
    stored = agent._session_db.get_messages('chat')
    assert [m['role'] for m in stored] == ['user', 'assistant', 'tool', 'assistant']
    assert stored[-1]['display_kind'] == 'hidden'
    agent._session_db.close()
