"""Desktop's real switch boundary must not publish a one-turn allocation."""
import json
import sqlite3
from copy import deepcopy
from unittest.mock import MagicMock, patch

import pytest

from tests.agent import test_managed_switch as fixtures

destinations = fixtures.destinations
managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route
build = fixtures.build
from agent.model_routing_managed import verify_managed_agent
from tui_gateway import server


def allocation_dump(path):
    with sqlite3.connect(path) as db:
        return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1, 2').fetchall()
                for table in ('aliases', 'routes', 'counters')}


@pytest.fixture
def desktop(destinations, monkeypatch):
    # Only UI delivery/worker spawning and external SDK construction are replaced.
    monkeypatch.setattr(server, '_restart_slash_worker', lambda *args: None)
    monkeypatch.setattr(server, '_emit_session_info', lambda *args: None)
    monkeypatch.setattr(server, '_persist_live_session_system_prompt', lambda *args: None)
    with patch('model_tools.get_tool_definitions', return_value=[]), patch(
            'agent.anthropic_adapter.build_anthropic_client', side_effect=lambda *a, **k: MagicMock()):
        yield


def session_for(agent):
    from hermes_state import SessionDB
    agent._session_db = SessionDB()
    agent._ensure_db_session()
    return {'agent': agent, 'session_key': agent.session_id, 'history': [], 'source': 'desktop'}


@pytest.mark.parametrize('standing', ['class:deep', 'anthropic-deep'])
@pytest.mark.parametrize('temporary', ['z-anthropic-deep --provider anthropic', 'class:standard'])
def test_once_preserves_allocation_resume_and_runtime(desktop, tmp_path, standing, temporary):
    agent = build(standing)
    session = session_for(agent)
    server._persist_live_session_runtime(session)
    before_row = agent._session_db.get_session('pin')
    original = deepcopy(agent.model_routing)
    client, compressor = agent._anthropic_client, agent.context_compressor
    before = allocation_dump(tmp_path / 'model-routing.sqlite3')
    result = server._apply_model_switch('ui', session, temporary + ' --once', confirm_expensive_model=True)
    assert result['scope'] == 'once'
    verify_managed_agent(agent, {'model': agent.model})
    assert agent.model != original['selected_model']
    assert allocation_dump(tmp_path / 'model-routing.sqlite3') == before
    server._persist_live_session_runtime(session)
    row = agent._session_db.get_session('pin')
    assert row['model'] == before_row['model']
    assert json.loads(row['model_config'])['model_routing'] == original
    overrides = server._stored_session_runtime_overrides(row)
    resumed = build(overrides['model_override']['model'])
    assert resumed.model_routing == original
    server._restore_agent_model_runtime(agent, session.pop('one_turn_model_restore'))
    assert agent.model_routing == original
    assert agent.model == original['selected_model']
    assert agent._anthropic_client is client
    assert agent.context_compressor is compressor
    assert compressor.model == agent.model
    assert agent._primary_runtime['model'] == agent.model
    verify_managed_agent(agent, {'model': agent.model})
    assert allocation_dump(tmp_path / 'model-routing.sqlite3') == before


@pytest.mark.parametrize('failure', ['client', 'compressor'])
def test_once_failure_is_noop(desktop, tmp_path, failure):
    agent = build('class:deep')
    session = session_for(agent)
    server._persist_live_session_runtime(session)
    before = allocation_dump(tmp_path / 'model-routing.sqlite3')
    original = deepcopy(agent.model_routing)
    client, compressor = agent._anthropic_client, agent.context_compressor
    seam = {'client': 'agent.anthropic_adapter.build_anthropic_client',
            'compressor': 'agent.agent_runtime_helpers._update_switch_compressor'}[failure]
    with patch(seam, side_effect=RuntimeError('construction failed')):
        with pytest.raises(ValueError, match='construction failed'):
            server._apply_model_switch('ui', session, 'class:standard --once', confirm_expensive_model=True)
    assert agent.model_routing == original
    assert agent._anthropic_client is client
    assert agent.context_compressor is compressor
    assert 'one_turn_model_restore' not in session
    assert allocation_dump(tmp_path / 'model-routing.sqlite3') == before
    verify_managed_agent(agent, {'model': agent.model})


@pytest.mark.parametrize('standing', ['class:deep', 'anthropic-deep'])
def test_temporary_class_failover_and_compression_keep_standing_choice(desktop, tmp_path, standing):
    from tests.agent.test_class_compression_lineage import rotate
    agent = build(standing)
    session = session_for(agent)
    original = deepcopy(agent.model_routing)
    client = agent._anthropic_client
    agent._cached_system_prompt = 'pre-compression standing prompt'
    before = allocation_dump(tmp_path / 'model-routing.sqlite3')
    server._apply_model_switch('ui', session, 'class:deep --once', confirm_expensive_model=True)
    assert agent._try_activate_fallback()
    assert agent.model == 'z-anthropic-deep'
    verify_managed_agent(agent, {'model': agent.model})
    assert not agent._try_activate_fallback()
    assert allocation_dump(tmp_path / 'model-routing.sqlite3') == before
    child = rotate(agent)
    session['session_key'] = child
    server._persist_live_session_runtime(session)
    row = agent._session_db.get_session(child)
    assert row['model'] == original['selected_model']
    assert json.loads(row['model_config'])['model_routing'] == original
    restarted = build('class:deep', child)
    assert restarted.model_routing == original
    after = allocation_dump(tmp_path / 'model-routing.sqlite3')
    assert after['routes'] == before['routes']
    assert after['counters'] == before['counters']
    server._restore_agent_model_runtime(agent, session.pop('one_turn_model_restore'))
    assert agent.session_id == child
    assert agent.context_compressor._session_id == child
    assert agent._cached_system_prompt is None
    assert agent.model_routing == original
    assert agent._anthropic_client is client
    client.close.assert_not_called()
    verify_managed_agent(agent, {'model': agent.model})


@pytest.mark.parametrize('blocked', ['paid --provider openrouter --once', 'class:deep --provider openrouter --once'])
def test_once_never_bypasses_subscription_policy(desktop, tmp_path, blocked):
    agent = build()
    session = session_for(agent)
    before = allocation_dump(tmp_path / 'model-routing.sqlite3')
    with pytest.raises(ValueError):
        server._apply_model_switch('ui', session, blocked, confirm_expensive_model=True)
    assert allocation_dump(tmp_path / 'model-routing.sqlite3') == before
    assert 'one_turn_model_restore' not in session
    verify_managed_agent(agent, {'model': agent.model})


def test_repeated_once_restores_original_and_exact_temporary_cannot_failover(desktop, tmp_path):
    agent = build('class:deep')
    session = session_for(agent)
    before = allocation_dump(tmp_path / 'model-routing.sqlite3')
    original = deepcopy(agent.model_routing)
    reasoning = deepcopy(agent.reasoning_config)
    for model in ('anthropic-standard', 'z-anthropic-deep'):
        server._apply_model_switch('ui', session, model + ' --provider anthropic --once --reasoning high',
                                   confirm_expensive_model=True)
        assert not agent._try_activate_fallback()
    server._restore_agent_model_runtime(agent, session.pop('one_turn_model_restore'))
    assert agent.model_routing == original
    assert agent.reasoning_config == reasoning
    assert allocation_dump(tmp_path / 'model-routing.sqlite3') == before


def test_class_confirmation_sees_concrete_route_without_allocation(desktop, tmp_path, monkeypatch):
    agent = build()
    session = session_for(agent)
    before = allocation_dump(tmp_path / 'model-routing.sqlite3')
    def confirm(result, *args):
        assert result.new_model == 'anthropic-standard'
        assert result.target_provider == 'anthropic'
        return {'value': result.new_model, 'confirm_required': True}
    monkeypatch.setattr(server, '_expensive_model_confirm', confirm)
    result = server._apply_model_switch('ui', session, 'class:standard --once')
    assert result['confirm_required']
    assert result['value'] == 'class:standard'
    assert agent.model == 'anthropic-deep'
    assert allocation_dump(tmp_path / 'model-routing.sqlite3') == before


@pytest.mark.parametrize('standing', ['class:deep', 'codex-deep'])
def test_cross_provider_once_restores_native_clients(desktop, destinations, tmp_path, standing):
    from hermes_cli.config import atomic_config_write
    route = deepcopy(destinations['model_routing']['routes']['deep'][0])
    route.update(provider='openai-codex', model='codex-deep')
    destinations['model_routing']['routes']['deep'].append(route)
    atomic_config_write(tmp_path / 'config.yaml', destinations)
    auth_path = tmp_path / 'auth.json'
    auth = json.loads(auth_path.read_text())
    auth['credential_pool']['openai-codex'] = [{
        'id': 'codex-test', 'label': 'test', 'priority': 0, 'auth_type': 'oauth',
        'source': 'manual', 'access_token': 'synthetic-codex-oauth'}]
    auth_path.write_text(json.dumps(auth))
    from run_agent import AIAgent
    with patch.object(AIAgent, '_create_openai_client',
                      side_effect=lambda kwargs, **kw: MagicMock(base_url=kwargs['base_url'])):
        agent = build(standing, 'cross')
        from hermes_cli.auth import DEFAULT_CODEX_BASE_URL
        if agent.provider == 'openai-codex':
            assert agent.base_url == DEFAULT_CODEX_BASE_URL, (agent.base_url, agent._client_kwargs)
        verify_managed_agent(agent, {'model': agent.model})
        session = session_for(agent)
        original = deepcopy(agent.model_routing)
        client, native = agent.client, agent._anthropic_client
        target = ('codex-deep --provider openai-codex' if agent.provider == 'anthropic'
                  else 'anthropic-standard --provider anthropic')
        before = allocation_dump(tmp_path / 'model-routing.sqlite3')
        server._apply_model_switch('ui', session, target + ' --once', confirm_expensive_model=True)
        verify_managed_agent(agent, {'model': agent.model})
        assert agent.provider != original['selected_provider']
        server._persist_live_session_runtime(session)
        row = agent._session_db.get_session('cross')
        config = json.loads(row['model_config'])
        snapshot = session['one_turn_model_restore']['runtime']
        assert (row['model'], config['provider'], config['base_url'], config['api_mode']) == (
            snapshot['model'], snapshot['provider'], snapshot['base_url'], snapshot['api_mode'])
        server._restore_agent_model_runtime(agent, session.pop('one_turn_model_restore'))
        assert agent.client is client
        assert agent._anthropic_client is native
        assert agent.context_compressor.model == agent.model
        assert agent.context_compressor.provider == agent.provider
        assert agent.model_routing == original
        verify_managed_agent(agent, {'model': agent.model})
        assert allocation_dump(tmp_path / 'model-routing.sqlite3') == before
