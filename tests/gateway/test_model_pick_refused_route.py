"""A route the managed subscription policy refuses must not block switching away from it."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent.model_routing import ModelRoutingError
from gateway.session_mutation_model import prepare_model
from gateway.session_policy import build_policy, restore_policy
from hermes_cli import model_switch
from hermes_state import SessionDB
from tests.agent import test_managed_subscription as fixtures

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


@pytest.mark.asyncio
async def test_pick_leaves_a_route_the_managed_policy_refuses(managed, tmp_path, monkeypatch):
    def refuse(**kwargs):
        raise ModelRoutingError('Managed subscription policy rejects unregistered or ambiguous concrete pin')

    seen = {}

    def switch(**kwargs):
        seen.update(kwargs)
        return model_switch.ModelSwitchResult(success=True, new_model='anthropic-deep', target_provider='anthropic',
                                              base_url='https://api.anthropic.com', api_mode='anthropic_messages')

    monkeypatch.setattr(model_switch, 'switch_model', switch)
    url = 'https://openrouter.ai/api/v1'
    config = {**managed, 'model': {'default': 'paid', 'provider': 'openrouter', 'base_url': url}}
    policy = build_policy({'cwd': str(tmp_path), 'model': 'paid', 'provider': 'openrouter', 'base_url': url},
                          config, private_secrets={})
    with SessionDB(tmp_path / 'state.db') as db:
        authority = SimpleNamespace(instance_id='i', epoch=1, profile_id='p', db=db)
        authority.runner = SimpleNamespace(session_authority=authority, _resolve_session_agent_runtime=refuse)
        result = await prepare_model(authority, SimpleNamespace(source=None, route='r'),
                                     {'model': 'anthropic-deep', 'provider': 'anthropic'},
                                     {'snapshot': {'receipt': {'session_id': 'sid', 'policy': asdict(policy)}}})
    moved = restore_policy(result['policy'])
    assert moved.model == 'anthropic-deep'
    assert moved.config()['model']['provider'] == 'anthropic'
    assert seen['current_model'] == 'paid' and seen['current_api_key'] == ''
