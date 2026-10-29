"""Managed cron fires reject old paid model/provider settings."""
from unittest.mock import patch

from tests.agent import test_managed_subscription as fixtures
import pytest

# Upstream admits agent cron runs through the canonical gateway owner (cdbe8c35adf); run_job
# here goes through the production owner execution seam, never a spawned gateway.
pytestmark = pytest.mark.usefixtures("cron_owner")

managed = fixtures.managed
routing_home = fixtures.routing_home
streaming_route = fixtures.streaming_route


def _run_paid_job():
    from cron.scheduler import run_job
    with patch('model_tools.get_tool_definitions', return_value=[]), \
         patch('cron.scheduler_delivery._resolve_origin', return_value=None), \
         patch('cron.scheduler._init_cron_mcp_tools'), \
         patch('hermes_cli.env_loader.load_hermes_dotenv'), \
         patch('hermes_cli.env_loader.reset_secret_source_cache'), \
         patch('httpx.Client.send') as send:
        success, _, _, error = run_job({'id': 'stale-paid-cron', 'name': 'paid cron',
                                       'model': 'vendor/paid', 'provider': 'openrouter', 'prompt': 'Say ok'})
    return success, error, send


def test_paid_cron_job_fails_without_send(managed):
    # [daveey] The preflight now refuses the stale pin as blocked_config (alert once) before any
    # runtime is built; the run still fails and nothing is sent.
    success, error, send = _run_paid_job()
    assert not success
    assert '[blocked_config]' in error and 'model_pin_not_in_routing_registry' in error
    send.assert_not_called()


def test_paid_cron_job_fails_without_send_when_preflight_is_off(managed, tmp_path):
    """``cron.preflight: false``: the runtime policy itself still refuses the pin with no send."""
    from hermes_cli.config import atomic_config_write
    managed['cron'] = {'preflight': False}
    atomic_config_write(tmp_path / 'config.yaml', managed)
    success, error, send = _run_paid_job()
    assert not success
    assert 'Managed subscription policy' in error
    send.assert_not_called()
