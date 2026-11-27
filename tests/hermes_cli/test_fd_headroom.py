"""Process-local headroom, with no live limit or Board mutations."""
import logging
from unittest.mock import Mock

import pytest
from hermes_cli import resource_limits as limits


@pytest.fixture
def guard(monkeypatch):
    limits._fd_samples.clear()
    limits._fd_status.clear()
    monkeypatch.setattr(limits, '_fd_last_check', None)
    monkeypatch.setattr(limits, '_fd_pid', limits.os.getpid())
    resource = Mock(RLIMIT_NOFILE=7, RLIM_INFINITY=-1)
    resource.getrlimit.return_value = (1000, 10000)
    monkeypatch.setattr(limits, '_resource', resource)
    snapshot = Mock(return_value=(500, {'other': 500}))
    notify = Mock(return_value={})
    monkeypatch.setattr(limits, '_fd_snapshot', snapshot)
    monkeypatch.setattr(limits, '_notify_fd_incident', notify)
    monkeypatch.setattr(limits, '_fd_core_head', lambda: 'a' * 40)
    return resource, snapshot, notify


def check(now=0, **kwargs):
    return limits.check_fd_headroom({'runtime': {'fd_headroom': {'interval_seconds': 1}}},
                                    role='gateway', now=now, **kwargs)


def test_ratio_trigger(guard, caplog):
    resource, _, notify = guard
    with caplog.at_level(logging.INFO):
        check()
    assert notify.call_count == 1
    assert 'fd-headroom:' in caplog.text
    assert '500' in caplog.text and 'gateway' in caplog.text and 'a' * 40 in caplog.text
    resource.setrlimit.assert_not_called()


def test_slope_trigger_and_below_both(guard):
    _, snapshot, notify = guard
    snapshot.return_value = (100, {})
    check(0)
    check(1800)
    notify.assert_not_called()
    snapshot.return_value = (200, {})
    check(3600)  # +100 / 30 min => exhaustion in four hours, below ratio
    assert notify.call_count == 1
    check(7201)  # old growth has fallen out of the 30-minute window
    assert notify.call_count == 1


@pytest.mark.parametrize(('hard', 'expected'), [(10000, 4000), (2000, 2000), (-1, 4000)])
def test_raise_soft_and_hard_clamp(guard, hard, expected):
    resource, snapshot, notify = guard
    resource.getrlimit.return_value = (1000, hard)
    snapshot.return_value = (750, {'resource-leases': 750})
    check()
    resource.setrlimit.assert_called_once_with(7, (expected, hard))
    assert f'raised soft 1000→{expected}' in notify.call_args.args[0]['raised']


def test_raise_failure_does_not_suppress_incident(guard):
    resource, snapshot, notify = guard
    snapshot.return_value = (800, {})
    resource.setrlimit.side_effect = OSError('denied')
    check()
    assert notify.call_count == 1
    assert 'failed' in notify.call_args.args[0]['raised']


def test_never_raises_and_due_gate(guard):
    _, snapshot, notify = guard
    notify.side_effect = RuntimeError('courier down')
    check()
    check(0.5)
    assert snapshot.call_count == 1
    check(1)
    assert snapshot.call_count == 2


@pytest.mark.parametrize('directory', ['/proc/self/fd', '/dev/fd'])
def test_fd_directory_and_census(monkeypatch, directory):
    targets = ['/.resource-leases/a', '/data/state.db', '/data/state.db-wal',
               '/data/state.db-shm (deleted)', 'socket:[123]', 'pipe:[456]', '/data/log']
    def listing(path):
        if path != directory:
            raise FileNotFoundError(path)
        return [str(i) for i in range(len(targets))]
    monkeypatch.setattr(limits.os, 'listdir', listing)
    monkeypatch.setattr(limits.os, 'readlink', lambda p: targets[int(p.rsplit('/', 1)[1])])
    count, census = limits._fd_snapshot(20)
    assert count == 7
    assert census == {'resource-leases': 1, 'sqlite': 3, 'socket': 1, 'pipe': 1, 'other': 1}


def test_below_quarter_avoids_readlinks(monkeypatch):
    monkeypatch.setattr(limits.os, 'listdir', lambda _: ['1', '2'])
    links = Mock(side_effect=AssertionError('unneeded census'))
    monkeypatch.setattr(limits.os, 'readlink', links)
    assert limits._fd_snapshot(100) == (2, {})
    links.assert_not_called()


def test_disabled_and_infinite_soft(guard):
    resource, snapshot, notify = guard
    limits.check_fd_headroom({'runtime': {'fd_headroom': {'enabled': False}}})
    snapshot.assert_not_called()
    resource.getrlimit.return_value = (-1, -1)
    check()
    notify.assert_not_called()


def test_config_defaults():
    assert limits.DEFAULT_CONFIG['runtime']['fd_headroom'] == {
        'enabled': True, 'warn_ratio': 0.5, 'raise_ratio': 0.75,
        'horizon_hours': 6, 'interval_seconds': 300}
