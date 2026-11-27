"""Process-local headroom baseline/regression behavior; no live limits or Board writes."""
import logging
from unittest.mock import Mock

import pytest
from hermes_cli import resource_limits as limits


@pytest.fixture
def guard(monkeypatch):
    limits._fd_samples.clear()
    limits._fd_status.clear()
    monkeypatch.setattr(limits, '_fd_incident', {})
    monkeypatch.setattr(limits, '_fd_last_check', None)
    monkeypatch.setattr(limits, '_fd_pid', limits.os.getpid())
    resource = Mock(RLIMIT_NOFILE=7, RLIM_INFINITY=-1)
    resource.getrlimit.return_value = (1000, 10000)
    monkeypatch.setattr(limits, '_resource', resource)
    snapshot = Mock(return_value=(500, {'classes': {'other': 500}}))
    notify = Mock(return_value={})
    monkeypatch.setattr(limits, '_fd_snapshot', snapshot)
    monkeypatch.setattr(limits, '_notify_fd_incident', notify)
    monkeypatch.setattr(limits, '_fd_core_head', lambda: 'a' * 40)
    return resource, snapshot, notify


def check(now=0, **kwargs):
    return limits.check_fd_headroom({}, role='gateway', now=now, **kwargs)


def test_below_both_updates_status_without_warning_card_or_limit_change(guard, caplog):
    resource, snapshot, notify = guard
    snapshot.return_value = (100, {})
    with caplog.at_level(logging.WARNING):
        check(0)
        check(300)
    assert 'fd-headroom:' not in caplog.text
    notify.assert_not_called()
    resource.setrlimit.assert_not_called()
    state = limits.fd_headroom_status()
    assert state['sampled_at'] > 0
    assert (state['count'], state['soft'], state['hard'], state['ratio']) == (100, 1000, 10000, 0.1)
    assert state['last_card_id'] is None and state['create_state'] == 'ok'


def test_ratio_trigger(guard, caplog):
    resource, _, notify = guard
    with caplog.at_level(logging.WARNING):
        check()
    assert notify.call_count == 1
    assert 'fd-headroom:' in caplog.text
    assert '500' in caplog.text and 'gateway' in caplog.text and 'a' * 40 in caplog.text
    resource.setrlimit.assert_not_called()


def test_never_raises_and_due_gate(guard):
    _, snapshot, notify = guard
    notify.side_effect = RuntimeError('courier down')
    check(0)
    check(299)
    assert snapshot.call_count == 1
    check(300)
    assert snapshot.call_count == 2
    assert limits.fd_headroom_status()['error_class'] == 'RuntimeError'


def test_disabled_and_infinite_soft(guard):
    resource, snapshot, notify = guard
    limits.check_fd_headroom({'runtime': {'fd_headroom': {'enabled': False}}})
    snapshot.assert_not_called()
    resource.getrlimit.return_value = (-1, -1)
    check()
    notify.assert_not_called()


def test_accepted_pending_create_reconciles_on_healthy_tick(guard):
    _, snapshot, notify = guard
    notify.return_value = {'envelope': {'id': 'same-incident'}, 'task_id': None}
    check(0)
    snapshot.return_value = (100, {})
    check(300)
    assert notify.call_count == 2


def test_config_defaults():
    assert limits.DEFAULT_CONFIG['runtime']['fd_headroom'] == {
        'enabled': True, 'warn_ratio': 0.5, 'raise_ratio': 0.75,
        'horizon_hours': 6, 'interval_seconds': 300, 'max_soft': 262144}
