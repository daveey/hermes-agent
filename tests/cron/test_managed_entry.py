"""Managed cron fires reject old paid model/provider settings."""
from unittest.mock import patch

from tests.agent import test_managed_subscription as fixtures

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


def test_paid_cron_job_fails_without_send(managed):
    from cron.scheduler import run_job
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('cron.scheduler_delivery._resolve_origin', return_value=None), \
         patch('cron.scheduler._init_cron_mcp_tools'), \
         patch('hermes_cli.env_loader.load_hermes_dotenv'), \
         patch('hermes_cli.env_loader.reset_secret_source_cache'), \
         patch('httpx.Client.send') as send:
        success, _, _, error = run_job({'id': 'stale-paid-cron', 'name': 'paid cron',
                                       'model': 'vendor/paid', 'provider': 'openrouter', 'prompt': 'Say ok'})
        assert not success
        assert 'Managed subscription policy' in error
        send.assert_not_called()
