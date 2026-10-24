"""Managed send/switch regressions found during independent review."""
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from agent import auxiliary_client as aux
from hermes_cli.config import atomic_config_write
from hermes_state import SessionDB
from tests.agent import test_managed_switch as switches
from tests.agent import test_managed_auxiliary as auxiliary

managed = switches.managed
routing_home = switches.routing_home
streaming_route = switches.streaming_route
destinations = switches.destinations
native_transport = auxiliary.native_transport


def test_switch_with_real_session_database(destinations, tmp_path):
    db = SessionDB(tmp_path / 'sessions.db')
    db.create_session('pin', source='cli', model='anthropic-deep')
    try:
        with patch('model_tools.get_tool_definitions', return_value=[]), \
             patch('agent.anthropic_adapter.build_anthropic_client', return_value=MagicMock()):
            agent = switches.build('class:deep')
            agent._session_db = db
            agent._session_db_created = True
            agent.context_compressor.bind_session_state(db, 'pin')
            agent.switch_model('z-anthropic-deep', 'anthropic')
            assert agent.model == 'z-anthropic-deep'
            assert agent.context_compressor._session_db is db
            assert db.get_session('pin')['model'] == 'z-anthropic-deep'
    finally:
        db.close()


@pytest.mark.parametrize('surface', ['main', 'auxiliary'])
def test_class_route_shared_between_classes_sends(managed, tmp_path, native_transport, surface):
    managed['model_routing']['routes']['standard'] = deepcopy(managed['model_routing']['routes']['deep'])
    atomic_config_write(tmp_path / 'config.yaml', managed)
    if surface == 'auxiliary':
        result = aux.call_llm(model='class:deep', provider='auto', messages=[{'role': 'user', 'content': 'ok'}])
        assert result.choices[0].message.content == 'ok'
    else:
        with patch('model_tools.get_tool_definitions', return_value=[]), \
             patch('agent.title_generator._auto_title_enabled', return_value=False):
            agent = switches.build('class:deep')
            try:
                result = agent.run_conversation('ok')
                assert result['final_response'] == 'ok', result
            finally:
                agent.release_clients()
    assert native_transport
    assert native_transport[0][2]['model'] == 'anthropic-deep'
