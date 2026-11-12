"""Gateway-owned turns honor a core-update pause at the next completed-tool boundary, only when bound."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent.turn_iteration_prep import _step_boundary_pause
from gateway.config import Platform
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.session_update_pause_hook import bind_scope, current_scope, fence_root, installed, is_paused
from gateway.turn_context import TurnContext
from hermes_cli import admission_fence
from tui_gateway.update_pause import PAUSE_REASON

HISTORY = [
    {'role': 'user', 'content': 'continue the task'},
    {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'call-1', 'type': 'function', 'function': {'name': 'example', 'arguments': '{}'}}]},
    {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'already done'},
]


class Agent:
    """Controlled agent whose run_conversation consults the REAL boundary helper mid-turn."""
    step_boundary_pause = None

    def __init__(self):
        self.entered, self.boundary, self.finish = (threading.Event() for _ in range(3))
        self.reason = 'unset'
        self.seen_hook = None

    def run_conversation(self, message, **kwargs):
        self.entered.set()
        self.seen_hook = self.step_boundary_pause
        assert self.boundary.wait(10), 'test never released the completed tool boundary'
        self.reason = _step_boundary_pause(self, HISTORY, 1)
        if self.reason:
            return {'paused_for_update': True, 'interrupted': True, 'completed': False,
                    'messages': HISTORY, 'final_response': ''}
        # A real unpaused turn would make its next model call; hold it so completion is not mistaken for parking.
        assert self.finish.wait(10), 'test never released the unpaused model call'
        return {'messages': HISTORY, 'final_response': 'finished'}


def _turn():
    return TurnRunner(
        SimpleNamespace(session_authority=None, _consume_pending_native_image_paths=lambda key: []),
        TurnContext(message='continue the task', session_id='chat', session_key='agent:main:local:dm:chat',
                    source=SessionSource(platform=Platform.LOCAL, chat_id='chat', user_id='u')))


def _run(agent, root, *, scope_home=None, eligible=True, request=True, before_boundary=None):
    """Run one turn through the real runner call seam; request the pause mid-turn."""
    turn = _turn()

    def go():
        if scope_home is None:
            return turn._run_conversation_with_approval(agent, [], None, None, None)
        with bind_scope(scope_home, eligible=eligible):
            return turn._run_conversation_with_approval(agent, [], None, None, None)

    with ThreadPoolExecutor(max_workers=1) as executor:
        running = executor.submit(go)
        try:
            assert agent.entered.wait(10)
            if request:
                admission_fence.set_hold(root)
                admission_fence.request_pause(root, expires_at=time.time() + 60)
                assert admission_fence.pause_requested(root)
            if before_boundary:
                before_boundary()
            agent.boundary.set()
            deadline = time.time() + 5
            while agent.reason == 'unset' and time.time() < deadline:
                time.sleep(0.01)
            agent.finish.set()
            return running.result(timeout=10)
        finally:
            agent.boundary.set()
            agent.finish.set()
            admission_fence.clear_hold(root)
            admission_fence.clear_pause_request(root)


def test_gateway_turn_reaches_update_checkpoint_at_next_tool_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent = Agent()
    result = _run(agent, tmp_path, scope_home=tmp_path)
    assert agent.reason == PAUSE_REASON
    assert is_paused(result) and result['paused_for_update'] is True
    assert agent.step_boundary_pause is None  # prior (class default) restored


def test_named_profile_home_uses_owning_root_fence(tmp_path, monkeypatch):
    profile = tmp_path / 'profiles' / 'work'
    profile.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(profile))
    assert fence_root(profile) == tmp_path
    agent = Agent()
    result = _run(agent, tmp_path, scope_home=profile)  # request lives in the root, not the profile
    assert result['paused_for_update'] is True
    # A request under the profile dir alone is not the owning fence.
    agent2 = Agent()
    result2 = _run(agent2, profile, scope_home=profile)
    assert 'paused_for_update' not in result2 and agent2.reason is None


def test_no_pause_request_leaves_turn_unaffected(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent = Agent()
    result = _run(agent, tmp_path, scope_home=tmp_path, request=False)
    assert agent.reason is None
    assert result == {'messages': HISTORY, 'final_response': 'finished'}
    assert callable(agent.seen_hook)  # installed during the run, gone after
    assert agent.step_boundary_pause is None


@pytest.mark.parametrize('scope', ['unbound', 'ineligible'])
def test_unsupported_turn_is_never_parked(tmp_path, monkeypatch, scope):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent = Agent()
    kw = {'scope_home': None} if scope == 'unbound' else {'scope_home': tmp_path, 'eligible': False}
    result = _run(agent, tmp_path, **kw)
    assert agent.reason is None and agent.seen_hook is None
    assert result['final_response'] == 'finished'


def test_scope_is_context_local(tmp_path):
    assert current_scope() is None
    with bind_scope(tmp_path, eligible=True) as scope:
        assert current_scope() is scope
    assert current_scope() is None


def test_prior_hook_restored_after_run_and_failure(tmp_path):
    agent = Agent()
    prior = lambda: None
    agent.step_boundary_pause = prior
    with bind_scope(tmp_path, eligible=True):
        with installed(agent) as on:
            assert on and agent.step_boundary_pause is not prior
        assert agent.step_boundary_pause is prior
        with pytest.raises(RuntimeError):
            with installed(agent):
                raise RuntimeError('boom')
    assert agent.step_boundary_pause is prior
    bare = SimpleNamespace()
    with bind_scope(tmp_path, eligible=True), installed(bare):
        assert callable(bare.step_boundary_pause)
    assert not hasattr(bare, 'step_boundary_pause')


def test_user_stop_wins_over_pause(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent = Agent()
    result = _run(agent, tmp_path, scope_home=tmp_path,
                  before_boundary=lambda: setattr(agent, '_interrupt_requested', True))
    assert agent.reason is None and 'paused_for_update' not in result


def test_pending_steer_wins_over_pause(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent = Agent()
    result = _run(agent, tmp_path, scope_home=tmp_path,
                  before_boundary=lambda: setattr(agent, '_pending_steer', 'actually do X'))
    assert agent.reason is None and 'paused_for_update' not in result


def test_system_issued_interrupt_does_not_block_pause(tmp_path, monkeypatch):
    # The update pause's own mid-tool cancel is an interrupt with a system reason; it must still pause.
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    agent = Agent()

    def mark():
        agent._interrupt_requested = True
        agent._tool_interrupt_reason = 'hermes update pause'
    result = _run(agent, tmp_path, scope_home=tmp_path, before_boundary=mark)
    assert agent.reason == PAUSE_REASON and result['paused_for_update'] is True


def test_paused_result_does_not_finish_stream_consumer():
    turn = _turn()
    turn._ctx.result_holder = [None]
    consumer = SimpleNamespace(finish=lambda *a: pytest.fail('terminal stream finish on pause'))
    result = {'paused_for_update': True, 'interrupted': True, 'final_response': ''}
    turn._finish_stream_consumer(result, [], consumer)
    assert turn._ctx.result_holder[0] is result
    done = []
    turn._finish_stream_consumer({'final_response': 'x'}, [], SimpleNamespace(finish=lambda *a: done.append(a)))
    assert done


def test_paused_projection_is_empty_nonterminal():
    from gateway.run_turn_runner import project_paused_result
    common = {'messages': HISTORY, 'error': None, 'interrupted': True}
    assert project_paused_result({'paused_for_update': True, 'final_response': 'MEDIA:/x.png'}, common) == {
        'final_response': '', 'paused_for_update': True, **common}
    assert project_paused_result({'final_response': ''}, common) is None
    assert project_paused_result({'paused_for_update': 'yes'}, common) is None
