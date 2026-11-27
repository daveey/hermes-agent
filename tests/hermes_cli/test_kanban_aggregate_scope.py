"""All per-run budgets share a finite pool, leaving the owner outside it."""
import subprocess
from unittest.mock import patch

import pytest


def test_non_systemd_command_is_unchanged():
    from hermes_cli.kanban_aggregate_scope import bounded_scope
    command = ['python', '-m', 'worker']
    with patch('subprocess.run') as run:
        assert bounded_scope(command) == command
        run.assert_not_called()


def test_pool_budget_is_configured_before_returning_scoped_command():
    from hermes_cli.kanban_aggregate_scope import bounded_scope, configure_pool
    configure_pool.cache_clear()
    command = ['systemd-run', '--user', '--scope', '--unit=x.scope', '--', 'worker']
    from tools.process_registry import systemd_user_bus_env
    env = systemd_user_bus_env()
    with patch('subprocess.run') as run:
        got = bounded_scope(command)
        run.assert_called_once_with(
            ['systemctl', '--user', 'set-property', '--runtime', 'hermes-kanban.slice',
             'MemoryHigh=70%', 'MemoryMax=80%'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10, env=env)
        assert '--slice=hermes-kanban.slice' in got[:got.index('--')]
        assert got[got.index('--') + 1:] == ['worker']
        bounded_scope(command)
        assert run.call_count == 1
    configure_pool.cache_clear()


def test_unavailable_budget_fails_closed_and_retries_later():
    from hermes_cli.kanban_aggregate_scope import bounded_scope, configure_pool
    configure_pool.cache_clear()
    command = ['systemd-run', '--user', '--scope', '--', 'worker']
    from tools.process_registry import RestartSafeScopeUnavailable

    with patch('subprocess.run', side_effect=subprocess.CalledProcessError(1, 'systemctl')) as run, \
            patch('time.sleep') as sleep:
        for _ in range(2):
            with pytest.raises(RestartSafeScopeUnavailable, match='aggregate'):
                bounded_scope(command)
        assert run.call_count == 6
        assert [call.args for call in sleep.call_args_list] == [(1,), (3,), (1,), (3,)]
    configure_pool.cache_clear()


@pytest.mark.parametrize('stderr', [b'manager reloading', 'manager reloading'])
def test_pool_failure_reports_host_returncode_and_bounded_stderr(stderr, caplog):
    from hermes_cli.kanban_aggregate_scope import configure_pool
    from tools.process_registry import RestartSafeScopeUnavailable

    configure_pool.cache_clear()
    suffix = b'x' * 600 if isinstance(stderr, bytes) else 'x' * 600
    error = subprocess.CalledProcessError(7, 'systemctl', stderr=stderr + suffix)
    with patch('subprocess.run', side_effect=error) as run, \
            patch('socket.gethostname', return_value='test-host'), patch('time.sleep'):
        with pytest.raises(RestartSafeScopeUnavailable) as caught:
            configure_pool()
    assert run.call_count == 3
    assert caught.value.__cause__ is error
    message = str(caught.value)
    assert 'test-host' in message
    assert 'returncode=7' in message
    assert 'stderr=' + ('manager reloading' + 'x' * 600)[:500] in message
    assert 'x' * 500 not in message
    assert len(caplog.records) == 3
    assert all(record.levelname == 'WARNING' and message in record.message
               for record in caplog.records)
    configure_pool.cache_clear()


@pytest.mark.parametrize('failures', [1, 2])
def test_transient_pool_failure_recovers_and_caches(failures):
    from hermes_cli.kanban_aggregate_scope import configure_pool

    configure_pool.cache_clear()
    error = subprocess.CalledProcessError(1, 'systemctl', stderr=b'busy')
    with patch('subprocess.run', side_effect=[error] * failures + [None]) as run, \
            patch('time.sleep') as sleep:
        configure_pool()
        configure_pool()
    assert run.call_count == failures + 1
    assert [call.args for call in sleep.call_args_list] == [(1,), (3,)][:failures]
    configure_pool.cache_clear()


@pytest.mark.parametrize('error', [OSError('no manager'),
                                  subprocess.TimeoutExpired('systemctl', 10, stderr=b'busy')])
def test_other_pool_errors_remain_bounded_and_fail_closed(error):
    from hermes_cli.kanban_aggregate_scope import configure_pool
    from tools.process_registry import RestartSafeScopeUnavailable

    configure_pool.cache_clear()
    with patch('subprocess.run', side_effect=error) as run, patch('time.sleep'):
        with pytest.raises(RestartSafeScopeUnavailable, match='returncode=None'):
            configure_pool()
    assert run.call_count == 3
    configure_pool.cache_clear()
