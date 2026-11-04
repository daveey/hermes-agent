"""Exercise the launch boundary, not just a hand-made scope classification."""
import pytest

from cron import scheduler
from tests.cron import test_restart_safe_worker as helpers
from tools.process_registry import GatewayChildDispatch


@pytest.mark.parametrize('no_agent', [False, True])
@pytest.mark.parametrize('mode', ['scoped', 'degraded'])
def test_external_firer_safety_depends_on_actual_executor(tmp_path, monkeypatch, no_agent, mode):
    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr('tools.process_registry.restart_safe_gateway_child_argv',
                        lambda command, **kw: GatewayChildDispatch(mode, command))
    helpers._stub_external_worker_launch(scheduler, monkeypatch)
    dropped = []
    monkeypatch.setattr(scheduler, '_drop_run_reservation', dropped.append)
    key = scheduler._inflight_key('job-1')
    monkeypatch.setattr(scheduler, '_running_job_ids', {key})
    monkeypatch.setattr(scheduler, '_running_since', {})
    monkeypatch.setattr(scheduler, '_scope_isolated_job_ids', set())
    monkeypatch.setattr(scheduler, '_running_worker_pids', {})
    monkeypatch.setattr(scheduler, '_restart_safe_waiter_job_ids', set())
    monkeypatch.setattr(scheduler, '_running_fire_owners', {})
    safe = no_agent and mode == 'scoped'

    def wait(*args, **kwargs):
        assert scheduler.get_restart_wait_cron_counts() == {
            'awaitable': int(not safe), 'wedged': 0, 'restart_safe': int(safe)}
        assert scheduler.get_running_job_details()[0]['restart_safe'] is safe
        assert dropped == ([key] if safe else [])
        # Even an owner-dependent observer has its own ledger tail; shutdown must
        # not steal that tail from the surviving external process.
        assert key in scheduler._restart_safe_waiter_job_ids
        return True

    monkeypatch.setattr(scheduler, '_wait_for_external_cron_worker', wait)
    assert scheduler._launch_external_cron_worker(
        {'id': 'job-1', 'execution_id': 'exec-1', 'no_agent': no_agent, 'prompt': 'work'})
