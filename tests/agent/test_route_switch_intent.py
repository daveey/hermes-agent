"""A committed explicit model switch replaces the recorded allocation at the next build, once. [daveey]

Incident: the canonical ``model`` mutation moved ``sessions.model`` to claude-opus while the allocation
store (and ``model_config.model_routing``) still held an exact pin on gpt-astra, so the next build either
refused ("Exact pinned route cannot fail over") or a reader restored the OLD pin.
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.model_routing import ModelRoutingError
from agent.model_routing_state import RoutingState
from agent.model_routing_switch_intent import PREFIX, record_in_transaction
from hermes_state import SessionDB
from run_agent import AIAgent
from tests.agent import test_managed_switch as switch_fixtures

managed = switch_fixtures.managed
routing_home = switch_fixtures.routing_home
streaming_route = switch_fixtures.streaming_route
destinations = switch_fixtures.destinations


def _build(model, session='pin', session_db=None):
    return AIAgent(model=model, provider='auto', session_id=session, session_db=session_db,
                   quiet_mode=True, skip_memory=True, skip_context_files=True)


@pytest.fixture
def seams():
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('agent.anthropic_adapter.build_anthropic_client', return_value=MagicMock()):
        yield


def _commit_intent(path, session, model, provider='anthropic'):
    db = SessionDB(path)
    db._execute_write(lambda conn: record_in_transaction(conn, session, model, provider, 2))
    db.close()


def test_committed_switch_survives_restart_and_replaces_the_exact_pin(destinations, tmp_path, seams):
    _build('anthropic-deep')
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    assert state.peek('pin', 'deep')['selected_model'] == 'anthropic-deep'
    _commit_intent(tmp_path / 'state-under-test.db', 'pin', 'z-anthropic-deep')
    restarted = SessionDB(tmp_path / 'state-under-test.db')  # a fresh owner process reads the receipt
    try:
        agent = _build('z-anthropic-deep', session_db=restarted)
        assert agent.model == 'z-anthropic-deep'
        recorded = state.peek('pin', 'deep')
        assert (recorded['selected_model'], recorded['exact_pin'], recorded['reason']) == (
            'z-anthropic-deep', True, 'explicit-switch')
        assert restarted.get_meta(PREFIX + 'pin') is None  # one-shot
        # The new pin is now itself exact: the old model cannot come back without its own switch.
        with pytest.raises(ModelRoutingError, match='cannot fail over'):
            _build('anthropic-deep', session_db=restarted)
    finally:
        restarted.close()


def test_no_intent_cannot_override_an_exact_pin(destinations, tmp_path, seams):
    _build('anthropic-deep')
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    before = state.peek('pin', 'deep')
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        with pytest.raises(ModelRoutingError, match='cannot fail over'):
            _build('z-anthropic-deep', session_db=db)
        assert state.peek('pin', 'deep') == before
    finally:
        db.close()


@pytest.mark.parametrize('session,model', [('other', 'z-anthropic-deep'), ('pin', 'anthropic-standard')])
def test_an_intent_for_another_session_or_tuple_does_not_move_the_pin(destinations, tmp_path, seams, session, model):
    _build('anthropic-deep')
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    before = state.peek('pin', 'deep')
    _commit_intent(tmp_path / 'state-under-test.db', session, model)
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        with pytest.raises(ModelRoutingError, match='cannot fail over'):
            _build('z-anthropic-deep', session_db=db)
        assert state.peek('pin', 'deep') == before
        assert db.get_meta(PREFIX + session) is not None  # never consumed by a non-matching build
    finally:
        db.close()


def test_failed_allocation_rolls_back_and_keeps_the_intent(destinations, tmp_path, seams):
    _build('anthropic-deep')
    state = RoutingState(tmp_path / 'model-routing.sqlite3')
    before = state.peek('pin', 'deep')
    _commit_intent(tmp_path / 'state-under-test.db', 'pin', 'z-anthropic-deep')
    db = SessionDB(tmp_path / 'state-under-test.db')
    try:
        with patch('agent.model_routing_runtime.verify_class_runtime',
                   side_effect=ModelRoutingError('credential refused')):
            with pytest.raises(ModelRoutingError, match='credential refused'):
                _build('z-anthropic-deep', session_db=db)
        assert state.peek('pin', 'deep') == before
        assert db.get_meta(PREFIX + 'pin') is not None
        assert _build('z-anthropic-deep', session_db=db).model == 'z-anthropic-deep'
        assert db.get_meta(PREFIX + 'pin') is None
    finally:
        db.close()
