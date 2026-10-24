"""Temporary models restore compressor ownership and standing compression lineage."""
import json
from unittest.mock import patch

import pytest

from tests.tui_gateway import test_managed_model_once as fixtures
from tui_gateway import server
from hermes_cli.config import atomic_config_write
from tests.agent.test_class_compression_lineage import rotate
from agent.model_routing_state import RoutingState
from agent.conversation_compression import ensure_compression_feasibility_checked

# Real Desktop switch and AIAgent; synthetic credentials and SDK construction only.
destinations = fixtures.destinations
managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route
desktop = fixtures.desktop
build = fixtures.build
session_for = fixtures.session_for


def test_restore_rechecks_unprobed_standing_compressor(desktop):
    agent = build('class:deep')
    session = session_for(agent)
    agent._compression_feasibility_checked = False
    original = agent.context_compressor
    with patch('agent.conversation_compression.check_compression_model_feasibility') as probe:
        server._apply_model_switch('ui', session, 'class:standard --once', confirm_expensive_model=True)
        assert agent._compression_feasibility_checked is True
        server._restore_agent_model_runtime(agent, session.pop('one_turn_model_restore'))
        assert agent.context_compressor is original
        probe.reset_mock()
        ensure_compression_feasibility_checked(agent, 1000000)
        probe.assert_called_once_with(agent)


@pytest.mark.parametrize('standing', ['class:deep', 'anthropic-deep'])
def test_unmanaged_once_compression_uses_standing_lineage(desktop, destinations, tmp_path, standing):
    destinations['model_routing']['enforce_subscription_only'] = False
    atomic_config_write(tmp_path / 'config.yaml', destinations)
    agent = build(standing)
    session = session_for(agent)
    expected = agent.model_routing
    original_model = agent.model
    original_compressor = agent.context_compressor
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    before = fixtures.allocation_dump(state.path) if state.path.exists() else None
    target = ('anthropic-standard --provider anthropic' if expected else 'class:deep')
    server._apply_model_switch('ui', session, target + ' --once', confirm_expensive_model=True)
    assert bool(agent.model_routing) != bool(expected)
    for _ in range(2):
        child = rotate(agent)
        assert state.peek(child, 'deep') == expected
        session['session_key'] = child
        server._persist_live_session_runtime(session)
        row = agent._session_db.get_session(child)
        assert row['model'] == original_model
        assert json.loads(row['model_config']).get('model_routing') == expected
        restored = server._stored_session_runtime_overrides(row)
        resumed = build(restored['model_override']['model'], child)
        assert resumed.model == original_model
        assert resumed.model_routing == expected
    if before is None:
        assert not state.path.exists()
    else:
        after = fixtures.allocation_dump(state.path)
        assert after['routes'] == before['routes']
        assert after['counters'] == before['counters']
    server._restore_agent_model_runtime(agent, session.pop('one_turn_model_restore'))
    assert agent.context_compressor is original_compressor
    assert original_compressor._session_id == child
    assert agent.model == original_model
    assert agent.model_routing == expected


@pytest.mark.parametrize('checked', [False, True])
@pytest.mark.parametrize('notice', [None, 'standing warning'])
@pytest.mark.parametrize('fail', [False, True])
def test_once_restores_probe_warning_and_handles(desktop, tmp_path, checked, notice, fail):
    agent = build('class:deep')
    session = session_for(agent)
    agent._compression_feasibility_checked = checked
    agent._consecutive_stale_streams = 2
    agent._compression_warning = notice
    if notice is not None:
        agent._last_feasibility_notice = notice
    else:
        agent.__dict__.pop('_last_feasibility_notice', None)
    compressor, client, pool = agent.context_compressor, agent._anthropic_client, agent._credential_pool
    threshold = compressor.threshold_tokens
    before = fixtures.allocation_dump(tmp_path / 'model-routing.sqlite3')

    def probe(staged):
        staged._last_feasibility_notice = 'temporary warning'
        staged._compression_warning = 'temporary warning'
        staged.context_compressor._aux_threshold_ceiling_tokens = 12345

    def reject(*args):
        raise RuntimeError('late switch failure')

    with patch('agent.conversation_compression.check_compression_model_feasibility', side_effect=probe):
        if fail:
            with patch('agent.agent_runtime_helpers._build_primary_runtime_snapshot', side_effect=reject):
                with pytest.raises(ValueError, match='late switch failure'):
                    server._apply_model_switch('ui', session, 'class:standard --once', confirm_expensive_model=True)
        else:
            server._apply_model_switch('ui', session, 'class:standard --once', confirm_expensive_model=True)
            assert agent._compression_warning == 'temporary warning'
            server._restore_agent_model_runtime(agent, session.pop('one_turn_model_restore'))
    assert agent._compression_feasibility_checked is checked
    assert agent._consecutive_stale_streams == 2
    assert agent._compression_warning == notice
    assert getattr(agent, '_last_feasibility_notice', None) == notice
    if notice is None:
        assert not hasattr(agent, '_last_feasibility_notice')
    assert agent.context_compressor is compressor
    assert compressor.threshold_tokens == threshold
    assert agent._anthropic_client is client
    assert agent._credential_pool is pool
    assert agent.model_routing['selected_model'] == agent.model
    assert '_one_turn_runtime' not in vars(agent)
    assert 'one_turn_model_restore' not in session
    assert fixtures.allocation_dump(tmp_path / 'model-routing.sqlite3') == before
