"""Managed delegation cannot inherit or request a paid route."""
from unittest.mock import MagicMock, patch

import pytest

from agent.model_routing import ModelRoutingError
from run_agent import AIAgent
from tools.delegate_tool_config import _resolve_delegation_credentials
from tests.agent import test_managed_subscription as fixtures

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


def test_paid_delegation_rejected_without_send(managed):
    from tools.delegate_tool import _build_child_agent, _credential_overrides
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.anthropic_adapter.build_anthropic_client', return_value=MagicMock()), \
         patch('tools.delegate_tool._load_config', return_value={}), patch('httpx.Client.send') as send:
        parent = AIAgent(model='class:deep', provider='auto', session_id='parent',
                         quiet_mode=True, skip_memory=True, skip_context_files=True)
        config = {'model': 'vendor/paid', 'provider': 'openrouter'}
        try:
            with pytest.raises((ModelRoutingError, ValueError), match='Managed subscription policy'):
                creds = _resolve_delegation_credentials(config, parent)
                _build_child_agent(task_index=0, goal='Say ok', context=None, toolsets=None,
                                   model=creds['model'], max_iterations=2, task_count=1,
                                   parent_agent=parent, **_credential_overrides(creds, config))
            send.assert_not_called()
        finally:
            parent.release_clients()
