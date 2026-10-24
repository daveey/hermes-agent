"""Concrete paid CLI settings and generated Kanban worker argv fail closed."""
from types import SimpleNamespace
from unittest.mock import patch

import cli
import pytest

from agent.model_routing import ModelRoutingError
from hermes_state import SessionDB
from tests.agent import test_managed_subscription as fixtures
from tests.hermes_cli.test_kanban_class_routing import worker_args

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


@pytest.mark.parametrize('surface', ['cli', 'kanban'])
def test_paid_entry_rejected_before_send(managed, tmp_path, monkeypatch, surface):
    model, provider = 'vendor/paid', 'openrouter'
    if surface == 'kanban':
        task = SimpleNamespace(id='stale-card', skills=[], reasoning_effort=None,
                               model_override=model, provider_override=provider)
        argv = worker_args(task, monkeypatch)
        model, provider = argv[argv.index('-m') + 1], argv[argv.index('--provider') + 1]
    db = SessionDB(tmp_path / 'worker.db')
    try:
        with patch('cli._prepare_deferred_agent_startup'), \
             patch('hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build'), \
             patch('model_tools.get_tool_definitions', return_value=[]), \
             patch('httpx.Client.send') as send:
            try:
                shell = cli.HermesCLI(model=model, provider=provider, compact=True)
                shell.session_id = 'managed-' + surface
                shell._session_db = db
                shell._install_tool_callbacks = lambda: None
                shell.streaming_enabled = False
                shell.ignore_rules = True
                assert not shell._init_agent()
            except ModelRoutingError:
                pass
            send.assert_not_called()
    finally:
        db.close()
