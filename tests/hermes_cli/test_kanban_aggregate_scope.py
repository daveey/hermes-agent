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
    with patch('subprocess.run') as run:
        got = bounded_scope(command)
        run.assert_called_once_with(
            ['systemctl', '--user', 'set-property', '--runtime', 'hermes-kanban.slice',
             'MemoryHigh=70%', 'MemoryMax=80%'], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
        assert '--slice=hermes-kanban.slice' in got[:got.index('--')]
        assert got[got.index('--') + 1:] == ['worker']
        bounded_scope(command)
        assert run.call_count == 1
    configure_pool.cache_clear()


def test_unavailable_budget_fails_closed_and_retries_later():
    from hermes_cli.kanban_aggregate_scope import bounded_scope, configure_pool
    configure_pool.cache_clear()
    command = ['systemd-run', '--user', '--scope', '--', 'worker']
    with patch('subprocess.run', side_effect=subprocess.CalledProcessError(1, 'systemctl')) as run:
        for _ in range(2):
            with pytest.raises(RuntimeError, match='aggregate'):
                bounded_scope(command)
        assert run.call_count == 2
    configure_pool.cache_clear()
