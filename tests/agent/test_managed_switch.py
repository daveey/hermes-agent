"""Exact managed intent survives restart and failed explicit switches."""
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from agent.model_routing_state import RoutingState
from hermes_cli.config import atomic_config_write
from run_agent import AIAgent
from tests.agent import test_managed_subscription as fixtures

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


@pytest.fixture
def destinations(managed, tmp_path):
    second = deepcopy(managed['model_routing']['routes']['deep'][0])
    second['model'] = 'z-anthropic-deep'
    managed['model_routing']['routes']['deep'].append(second)
    standard = deepcopy(second)
    standard['model'] = 'anthropic-standard'
    managed['model_routing']['routes']['standard'] = [standard]
    atomic_config_write(tmp_path / 'config.yaml', managed)
    return managed


def build(model='anthropic-deep', session='pin'):
    return AIAgent(model=model, provider='auto', session_id=session,
                   quiet_mode=True, skip_memory=True, skip_context_files=True)


def test_exact_intent_persisted_before_restart_and_compression(destinations, tmp_path):
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.anthropic_adapter.build_anthropic_client', return_value=MagicMock()):
        first = build()
        state = RoutingState(tmp_path / 'model-routing.sqlite3')
        assert state.peek('pin', 'deep')['exact_pin'] is True
        state.link_continuation('compressed', 'pin')
        resumed = build('class:deep', 'compressed')
        assert resumed.model == first.model
        assert resumed.model_routing['exact_pin'] is True
        assert resumed._try_activate_fallback() is False


@pytest.mark.parametrize('target,level', [('z-anthropic-deep', 'deep'), ('anthropic-standard', 'standard')])
@pytest.mark.parametrize('failure', [None, 'client', 'compressor'])
def test_managed_switch_commits_only_after_runtime_ready(destinations, tmp_path, target, level, failure):
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.anthropic_adapter.build_anthropic_client', return_value=MagicMock()):
        agent = build('class:deep')
        state = RoutingState(tmp_path / 'model-routing.sqlite3')
        before = state.peek('pin', level)
        old_model = agent.model
        old_metadata = deepcopy(agent.model_routing)
        old_client = agent._anthropic_client
        seam = {'client': 'agent.anthropic_adapter.build_anthropic_client',
                'compressor': 'agent.agent_runtime_helpers._update_switch_compressor'}
        if failure:
            with patch(seam[failure], side_effect=RuntimeError('construction failed')):
                with pytest.raises(RuntimeError, match='construction failed'):
                    agent.switch_model(target, 'anthropic')
            assert agent.model == old_model
            assert agent.model_routing == old_metadata
            assert agent._anthropic_client is old_client
            assert state.peek('pin', level) == before
        else:
            agent.switch_model(target, 'anthropic')
            assert agent.model == target
            assert state.peek('pin', level)['exact_pin'] is True
            assert state.peek('pin', level)['selected_model'] == target
            resumed = build('class:' + level)
            assert resumed.model == target
            assert resumed._try_activate_fallback() is False


@pytest.mark.parametrize('target', ['class:deep', 'class:standard'])
def test_explicit_class_switch_releases_exact_pin(destinations, tmp_path, target):
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.anthropic_adapter.build_anthropic_client', return_value=MagicMock()):
        agent = build()
        assert agent.model_routing['exact_pin'] is True
        agent.switch_model(target, 'auto')
        assert agent.model_routing['requested_class'] == target.split(':')[1]
        assert not agent.model_routing.get('exact_pin')
        assert RoutingState(tmp_path / 'model-routing.sqlite3').peek('pin', target.split(':')[1]) == agent.model_routing
        assert agent._fallback_chain == []


def test_managed_cross_provider_preparation_keeps_verified_pool(destinations):
    from agent.agent_runtime_helpers import _swap_switch_runtime
    from types import SimpleNamespace
    pool = object()
    agent = SimpleNamespace(_credential_pool=pool, model_routing={'exact_pin': True},
                            _read_reasoning_echo_from_config=lambda: False)
    with patch('agent.turn_recovery_codex.reset_codex_reasoning_replay'), \
         patch('agent.credential_pool.load_pool') as load, \
         patch('agent.agent_runtime_helpers._build_switched_client'), \
         patch('agent.agent_runtime_helpers.sync_credential_pool_entry_id'):
        _swap_switch_runtime(agent, 'native', 'openai-codex', 'oauth',
                             'https://chatgpt.com/backend-api/codex', 'codex_responses',
                             'anthropic', 'anthropic', 'openai-codex',
                             managed={'credential_pool': pool, 'model_routing': {'exact_pin': True}})
    load.assert_not_called()
    assert agent._credential_pool is pool


def test_switch_primary_snapshot_uses_destination_overrides(destinations):
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.anthropic_adapter.build_anthropic_client', return_value=MagicMock()):
        agent = build('class:deep')
        agent.request_overrides = {'old': True}
        def apply(destination, provider):
            destination.request_overrides = {'new': True}
        with patch('agent.agent_runtime_helpers._apply_switched_provider_request_overrides', side_effect=apply):
            agent.switch_model('z-anthropic-deep', 'anthropic')
        assert agent._primary_runtime['request_overrides'] == agent.request_overrides
        assert 'old' not in agent._primary_runtime['request_overrides']


def test_late_failed_switch_does_not_reset_durable_compressor(destinations, tmp_path):
    from agent import agent_runtime_helpers as runtime
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.anthropic_adapter.build_anthropic_client', return_value=MagicMock()):
        agent = build('class:deep')
        db = MagicMock()
        compressor = agent.context_compressor
        compressor._session_db, compressor._session_id = db, 'pin'
        compressor._ineffective_compression_count = 3
        before = RoutingState(tmp_path / 'model-routing.sqlite3').peek('pin', 'deep')
        original = runtime._update_switch_compressor

        def fail_after_update(*args):
            original(*args)
            raise RuntimeError('late failure')

        with patch.object(runtime, '_update_switch_compressor', side_effect=fail_after_update):
            with pytest.raises(RuntimeError, match='late failure'):
                agent.switch_model('z-anthropic-deep', 'anthropic')
        assert agent.context_compressor is compressor
        assert compressor._ineffective_compression_count == 3
        assert db.mock_calls == []
        assert RoutingState(tmp_path / 'model-routing.sqlite3').peek('pin', 'deep') == before
