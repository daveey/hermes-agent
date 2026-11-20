"""Switches must refuse a turn that won before the switch snapshot too."""
from unittest.mock import patch

import pytest

from agent import model_routing_managed
from agent.agent_runtime_switch import TurnAdmittedDuringSwitch
from tests.agent import test_managed_switch_turn_custody as custody

managed = custody.managed
routing_home = custody.routing_home
streaming_route = custody.streaming_route
destinations = custody.destinations
live = custody.live


@pytest.mark.parametrize('managed_route', [True, False])
def test_switch_refuses_already_admitted_turn(live, managed_route):
    agent, _db = live
    before = custody.runtime_of(agent)
    lease = custody.admit(agent)
    try:
        with patch('agent.model_routing_managed.managed_config',
                   wraps=model_routing_managed.managed_config) as config:
            if not managed_route:
                config.side_effect = lambda: None
            with pytest.raises(TurnAdmittedDuringSwitch, match='not applied'):
                agent.switch_model(custody.TARGET if managed_route else 'claude-sonnet-4-5',
                                   'anthropic', api_key='fixture-key',
                                   base_url='https://api.anthropic.com')
        assert custody.runtime_of(agent) == before
    finally:
        lease.release()
