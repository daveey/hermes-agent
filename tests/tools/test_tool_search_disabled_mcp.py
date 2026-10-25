"""A disabled MCP server is not an unknown tool spelling. [daveey]"""
import pytest
from tools.tool_search_validation import not_deferrable_error


@pytest.mark.parametrize('enabled', [False, 'false'])
def test_disabled_server_explains_config_not_name(monkeypatch, enabled):
    from tools import mcp_tool_config
    monkeypatch.setattr(mcp_tool_config, '_load_mcp_config', lambda: {
        'midbrain-memory': {'enabled': enabled, '_relay_note': 'secret must not echo'}})
    error = not_deferrable_error('mcp__midbrain_memory__memory_search')
    assert 'disabled' in error
    assert 'mcp_servers.midbrain-memory.enabled' in error
    assert 'Use tool_search' not in error
    assert 'secret' not in error


def test_unknown_server_retains_name_hint(monkeypatch):
    from tools import mcp_tool_config
    monkeypatch.setattr(mcp_tool_config, '_load_mcp_config', dict)
    assert 'not a known tool name' in not_deferrable_error('mcp__absent__search')


def test_enabled_server_does_not_claim_disabled(monkeypatch):
    from tools import mcp_tool_config
    monkeypatch.setattr(mcp_tool_config, '_load_mcp_config', lambda: {'example': {'enabled': True}})
    assert 'disabled' not in not_deferrable_error('mcp__example__missing')
