"""Focused failure memo must never cache runtime authority or live child rows."""
from types import SimpleNamespace

from tests.tui_gateway.test_subagent_snapshot import runtime as _runtime_fixture
from tests.tools.test_async_delegation_failed_surface import home as _home_fixture, _delegation

runtime = _runtime_fixture
home = _home_fixture


def test_failure_memo_keeps_authority_and_child_progress_live(runtime, home, monkeypatch):
    import time
    from tools import delegate_tool_registry as reg
    server, owner, transport, call = runtime
    _delegation('failed', status='error', ui='ui-owner', result={'status': 'error'}, completed_at=time.time())
    assert call('subagent.list')['result']['delegations'][0]['delegation_id'] == 'failed'
    reg._active_subagents['child'] = {
        'subagent_id': 'child', 'owner_session_id': 'ui-owner', 'owner_transport': transport,
        'owner_session_record': owner, 'tool_count': 1, 'status': 'running', 'started_at': 999,
        'agent': SimpleNamespace(session_id='child-session')}
    first = call('subagent.list')['result']['subagents'][0]
    assert first['tool_count'] == 1
    reg._active_subagents['child']['tool_count'] = 2
    assert call('subagent.list')['result']['subagents'][0]['tool_count'] == 2
    reg._active_subagents.clear()
    assert call('subagent.list')['result']['subagents'] == []
    assert 'error' in call('subagent.list', via=SimpleNamespace(write=lambda frame: True))
    server._sessions.clear()
    assert 'error' in call('subagent.list')
