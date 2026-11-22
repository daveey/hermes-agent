"""Registered stop-only public API and closed operator wire contracts."""
import json

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_operator_call as courier
from tests.hermes_cli import test_kanban_protect_stopped_local as support

board = support.board
fixture = support.fixture


def test_registered_stop_tool_uses_actual_unsynced_kernel(board, monkeypatch):
    from tools import kanban_tools
    from tools.registry import registry
    import toolsets
    from agent.transports import hermes_tools_mcp_server as native
    tool = next((item for item in kanban_tools._TOOLS if item[0] == 'kanban_protect_stopped'), None)
    assert registry is not None and tool is not None
    assert 'kanban_protect_stopped' in toolsets.TOOLSETS['kanban']['tools']
    assert 'kanban_protect_stopped' in native.EXPOSED_TOOLS
    actor, rid, session, target, _, args = fixture(board, monkeypatch)
    from contextlib import contextmanager
    @contextmanager
    def local(_):
        yield 'default', board
    monkeypatch.setattr(kanban_tools, '_board', local)
    with scoped_current_session_id(session):
        result = json.loads(tool[2](args))
    assert result['ok'] is True and result['terminal_for_run'] is False
    assert result['execution_authority'] is False
    assert kb.get_task(board, target).status == 'triage'
    assert kb.get_task(board, actor).current_run_id == rid


def test_protect_operator_wire_has_closed_stop_only_fields(board, monkeypatch):
    _, _, _, _, _, args = fixture(board, monkeypatch)
    raw = {'op': 'operator', 'action': 'protect_stopped', **args, 'expected_updated_at':1234567890}
    assert courier.normalize_op(0, raw) == raw
    import pytest
    with pytest.raises(ValueError):
        courier.normalize_op(0, {**raw, 'release': True})
    with pytest.raises(ValueError, match='integer target clock'):
        courier.normalize_op(0, {**raw, 'expected_updated_at': None})
