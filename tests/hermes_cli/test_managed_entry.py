"""Concrete paid CLI settings and generated Kanban worker argv fail closed.

Fork: upstream's cutover runs every CLI and Kanban turn in the gateway. The CLI hands its launch model to
``session.create`` (``cli_init_mixin._gateway_launch``) and the Kanban worker is
``-m hermes_cli.kanban_worker_client`` whose profile owner runs the card's ``owner_model_pin``; both reach
the gateway's ``_make_agent``. So "rejected before send" is asserted where the agent is now built.
"""
from types import SimpleNamespace
from unittest.mock import patch

import cli
import pytest

from agent.model_routing import ModelRoutingError
from hermes_cli.cli_init_mixin import _gateway_launch
from tests.agent import test_managed_subscription as fixtures
from tests.hermes_cli.test_kanban_class_routing import gateway_agent, owner_pin

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


@pytest.mark.parametrize('surface', ['cli', 'kanban'])
def test_paid_entry_rejected_before_send(managed, tmp_path, monkeypatch, surface):
    monkeypatch.chdir(tmp_path)
    model, provider = 'vendor/paid', 'openrouter'
    if surface == 'kanban':
        # Paid overrides are still refused at the real construction boundary.
        with patch('httpx.Client.send') as paid_send:
            with pytest.raises(ModelRoutingError):
                gateway_agent(monkeypatch, model, provider)
            paid_send.assert_not_called()
        task = SimpleNamespace(id='stale-card', skills=[], reasoning_effort=None, current_run_id=None,
                               model_override=model, provider_override=provider)
        model, provider = owner_pin(task)
        assert (model, provider) == ('class:standard', 'auto')
        assert (task.model_override, task.provider_override) == ('vendor/paid', 'openrouter')
        # The standard registry lacks streaming capability: class alone is not runnable.
        assert all(not r['capabilities'].get('streaming')
                   for r in managed['model_routing']['routes']['standard'])
    else:
        shell = cli.HermesCLI(model=model, provider=provider, compact=True)
        launch = _gateway_launch(shell)
        model, provider = launch['model'], launch['provider']
        assert (model, provider) == ('vendor/paid', 'openrouter')
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('httpx.Client.send') as send:
        with pytest.raises(ModelRoutingError):
            gateway_agent(monkeypatch, model, provider)
        send.assert_not_called()
