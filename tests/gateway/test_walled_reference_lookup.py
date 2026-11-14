"""Pre-turn reference lookup must carry the local session's routing identity."""
import logging
from types import SimpleNamespace

import pytest
from tests.agent import test_walled_default_pin as fixtures
from tests.agent.test_walled_default_pin import (
    ASTRA, SID, LEGACY_ECHO, _legacy_row, _wall, _gui_policy,
)

home = fixtures.home


@pytest.mark.asyncio
async def test_gui_at_reference_expands_before_turn_scope(home, monkeypatch, caplog):
    from gateway.config import Platform
    from gateway.run_turn_prepare import GatewayTurnPrepareMixin
    from gateway.run_model_context import inbound_model_context_length
    from gateway.session_context_refs import inbound_references
    from agent.model_routing_runtime import current_routing_session

    _legacy_row(home, LEGACY_ECHO)
    _wall(home, 'openai-codex', ASTRA, 110)
    policy = _gui_policy(home)
    source = SimpleNamespace(platform=Platform.LOCAL, chat_id=SID)
    monkeypatch.setattr('gateway.session_policy.policy_for_source', lambda *a: policy)
    monkeypatch.setattr('gateway.session_authorities.active_authority', lambda *a: None)
    runner = SimpleNamespace(_pre_agent_fallback_notice=None)
    runner._resolve_session_agent_runtime = GatewayTurnPrepareMixin._resolve_session_agent_runtime.__get__(runner)
    async def context(source, key):
        return await inbound_model_context_length(runner, source, key)
    runner._inbound_model_context_length = context
    (home / 'x').write_text('reference payload marker')
    assert current_routing_session() is None
    with caplog.at_level(logging.WARNING):
        expanded, refusal = await inbound_references(runner, source, SID, 'see @file:x')
    assert refusal is None
    assert 'reference payload marker' in expanded, caplog.text
    assert 'context reference expansion failed' not in caplog.text
    assert 'reason=no-session' not in caplog.text
    assert current_routing_session() is None


@pytest.mark.parametrize('ambient', [None, 'existing-owner'])
@pytest.mark.parametrize('raises', [False, True])
def test_policy_lookup_preserves_owner_and_restores_scope(home, monkeypatch, ambient, raises):
    from agent.model_routing_runtime import current_routing_session, routing_session
    from gateway.run_turn_prepare import _resolve_policy_agent_runtime

    policy = _gui_policy(home)
    monkeypatch.setattr('gateway.session_authorities.active_authority', lambda *a: None)
    monkeypatch.setattr('gateway.session_policy.route_launch_key', lambda *a: None)
    monkeypatch.setattr('gateway.run._runtime_agent_kwargs', lambda runtime: runtime)
    seen = []

    def route(*args, **kwargs):
        seen.append(current_routing_session())
        if raises:
            raise ValueError('controlled route failure')
        return {'provider': 'openai-codex'}, None

    monkeypatch.setattr('gateway.session_worker_construct.route_runtime', route)
    with routing_session(ambient):
        if raises:
            with pytest.raises(ValueError, match='controlled route failure'):
                _resolve_policy_agent_runtime(SimpleNamespace(), policy, session_id=SID)
        else:
            _resolve_policy_agent_runtime(SimpleNamespace(), policy, session_id=SID)
        assert current_routing_session() == ambient
    assert seen == [ambient or SID]
    assert current_routing_session() is None
