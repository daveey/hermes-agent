"""Desktop saved overrides are fenced by the real agent builder."""
from unittest.mock import patch

import pytest

from agent.model_routing import ModelRoutingError
from tests.agent import test_managed_subscription as fixtures
from tui_gateway import server

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


def test_desktop_saved_paid_override_never_sends(managed, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from hermes_cli.config import load_config
    monkeypatch.setattr(server, '_load_cfg', load_config)
    monkeypatch.setattr(server, '_resolve_startup_runtime', lambda: ('class:deep', 'auto'))
    monkeypatch.setattr(server, '_agent_cbs', lambda sid: {})
    monkeypatch.setattr(server, '_load_enabled_toolsets', lambda platform: [])
    monkeypatch.setattr(server, '_startup_system_prompt', lambda *a: '')
    monkeypatch.setattr(server, '_get_db', lambda: None)
    overrides = server._stored_session_runtime_overrides({
        'model': 'vendor/paid', 'model_config': {'model': 'vendor/paid', 'provider': 'openrouter'}})
    with patch('httpx.Client.send') as send, patch('model_tools.get_tool_definitions', return_value=[]):
        with pytest.raises(ModelRoutingError):
            server._make_agent('ui', 'stale-paid', **overrides)
        send.assert_not_called()
