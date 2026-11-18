"""Durable notices yield inside execution middleware, before the provider or any tool.

The pacing callback is a fixture; gateway submission, middleware dispatch, agent loop,
pause watcher and durable marker are real. No live provider or plugin is required.
"""
import threading
import time

import pytest

from hermes_cli import admission_fence as af
from hermes_cli.plugins import _delivery_manager
from tests.tui_gateway import test_update_pause_model_request as base
from tests.tui_gateway.test_update_pause_mid_tool import _InlineThread
from tui_gateway import server, update_pause as up
from tui_gateway.turn_marker import read_turn_marker


@pytest.mark.parametrize('recoverable', [True, False])
def test_notice_middleware_pause_and_resume(tmp_path, monkeypatch, recoverable):
    home = tmp_path
    agent = base._make_agent(tmp_path)
    agent.client.chat.completions.create.return_value = base._response()
    monkeypatch.setattr('agent.title_generator.maybe_auto_title', lambda *a, **k: None)
    monkeypatch.setattr(server, '_hermes_home', home)
    monkeypatch.setattr(server, '_load_cfg', lambda: base.CFG)
    for name, value in {'_emit': lambda *a, **k: None, '_wire_callbacks': lambda sid: None,
                        '_sync_agent_model_with_config': lambda sid, s: None,
                        '_session_cwd': lambda s: str(home), '_register_session_cwd': lambda s: None,
                        '_tts_stream_begin': lambda: None, '_sync_session_key_after_compress': lambda *a, **k: None,
                        '_get_usage': lambda a: {}}.items():
        monkeypatch.setattr(server, name, value)
    monkeypatch.setattr(up, 'start_in_place_watcher', lambda *a: None)
    monkeypatch.setattr(up, 'MID_TOOL_GRACE_S', 0.3)
    monkeypatch.setattr(up, 'MID_TOOL_POLL_S', 0.05)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def pacing(*, next_call, request, **context):
        entered.set()
        while not release.wait(0.05) and not agent._interrupt_requested:
            agent._touch_activity('fixture pacing')
        return next_call(request)

    manager = _delivery_manager()
    monkeypatch.setitem(manager._middleware, 'llm_execution', [pacing])
    # The gateway switches to its profile scope; use this isolated test manager in that scope too.
    monkeypatch.setattr('hermes_cli.plugins._delivery_manager', lambda: manager)
    session = {'agent': agent, 'session_key': 'session-key', 'history': [], 'history_lock': threading.RLock(),
               'history_version': 0, 'running': True, 'attached_images': [], 'image_counter': 0, 'cols': 80,
               'slash_worker': None, 'show_reasoning': False, 'tool_progress_mode': 'all', 'inflight_turn': None,
               'profile_home': str(home)}
    notice = 'Original notice: report the completed example task, without repeating its work.'
    result = {}
    real_run = agent.run_conversation

    def capture(*a, **kw):
        result['value'] = real_run(*a, **kw)
        return result['value']

    monkeypatch.setattr(agent, 'run_conversation', capture)

    def issue_pause():
        if entered.wait(10):
            af.set_hold(home)
            af.request_pause(home, expires_at=time.time() + 120)
        # Bound the negative control and join this producer before restoring fixtures.
        if not finished.wait(4):
            release.set()

    issuer = threading.Thread(target=issue_pause)
    issuer.start()
    try:
        server._run_prompt_submit('rid', 'ui-sid', session, notice, display_kind='kanban_notice',
                                  auto_continue=recoverable)
        thread = session.get('_run_thread')
        assert thread is not None
        thread.join(20)
        assert not thread.is_alive()
    finally:
        release.set()
        finished.set()
        issuer.join(15)
    assert not issuer.is_alive()
    assert entered.is_set(), 'must actually enter middleware wait before the pause'
    if not recoverable:
        assert not result['value'].get('paused_for_update')
        assert result['value']['final_response'] == 'done'
        agent._session_db.close()
        return
    assert result['value'].get('paused_for_update') == up.PAUSE_REASON
    assert not agent.client.chat.completions.create.called
    assert result['value']['api_calls'] == 0
    marker = read_turn_marker(home, 'session-key')
    assert marker['prompt'] == notice and marker['reason'] == up.PAUSE_REASON
    assert marker['attempts'] == 0
    assert {m['role'] for m in result['value']['messages']} == {'user'}
    # Exercise the cold-session scheduler with a fresh session object. This is not a process restart proof.
    af.clear_pause_request(home)
    af.clear_hold(home)
    monkeypatch.setattr(server.threading, 'Thread', _InlineThread)
    monkeypatch.setattr(server, '_start_agent_build', lambda *a: None)
    monkeypatch.setattr(server, '_wait_agent', lambda *a, **k: None)
    monkeypatch.setattr(server, '_ensure_active_session_slot', lambda *a, **k: None)
    submitted = []
    monkeypatch.setattr(server, '_run_prompt_submit', lambda rid, sid, s, text, **kw: submitted.append((text, kw)))
    fresh = {'agent': agent, 'session_key': 'session-key', 'history': [],
             'history_lock': threading.RLock(), 'running': False, 'profile_home': str(home)}
    assert server._maybe_schedule_auto_continue('fresh-sid', fresh, 'session-key') is not None
    assert len(submitted) == 1 and notice in submitted[0][0]
    assert fresh['_auto_continue_prompt'] == notice and fresh['_auto_continue_attempt'] == 0
    # Resume deliberately strips notice control metadata: the durable admission owns receipt identity.
    assert submitted[0][1]['display_kind'] == 'auto_continue'
    assert server._maybe_schedule_auto_continue('fresh-sid', fresh, 'session-key') is None
    assert len(submitted) == 1
    agent._session_db.close()
