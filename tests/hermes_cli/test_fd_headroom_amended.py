"""Decided guard boundaries; all clocks injected, no live limits or courier."""
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
    snapshot = Mock(return_value=(500, {}))
    notify = Mock(return_value={})
    monkeypatch.setattr(limits, '_fd_snapshot', snapshot)
    monkeypatch.setattr(limits, '_notify_fd_incident', notify)
    monkeypatch.setattr(limits, '_fd_core_head', lambda: 'a' * 40)
    return resource, snapshot, notify


def check(now=0, settings=None):
    return limits.check_fd_headroom({'runtime': {'fd_headroom': settings or {}}},
                                    role='gateway', now=now)


def test_slope_requires_four_samples_twenty_minutes(guard):
    _, snapshot, notify = guard
    for now, count in [(0, 420), (300, 440), (600, 460), (900, 480)]:
        snapshot.return_value = (count, {})
        check(now, settings={'warn_ratio': .7})
    notify.assert_not_called()
    snapshot.return_value = (500, {})
    sample = check(1200, settings={'warn_ratio': .7})
    assert sample['slope'] == 4  # fd/min, endpoint estimator
    assert notify.call_count == 1


@pytest.mark.parametrize('counts', [[200, 180, 160, 140], [100, 100, 100, 100]])
def test_slope_nonpositive_and_expired_window_off(guard, counts):
    _, snapshot, notify = guard
    for now, count in zip([0, 400, 800, 1200], counts, strict=True):
        snapshot.return_value = (count, {})
        check(now)
    check(3601)
    notify.assert_not_called()


def test_raise_precedes_delivery_and_requires_readback(guard, caplog):
    resource, snapshot, notify = guard
    snapshot.return_value = (800, {})
    events = []
    resource.getrlimit.side_effect = lambda _: (events.append('read') or
                                               ((1000, 10000) if len(events) == 1 else (4000, 10000)))
    resource.setrlimit.side_effect = lambda *_: events.append('set')
    notify.side_effect = lambda *a, **kw: events.append('deliver') or {}
    with caplog.at_level(logging.WARNING):
        check()
    assert events == ['read', 'set', 'read', 'deliver']
    assert limits.fd_headroom_status()['state'] == 'raised'
    assert 'raised soft 1000→4000' in caplog.text
    assert 'count=800 soft=1000' in caplog.text
    assert notify.call_args.args[0]['triggered'] is True


@pytest.mark.parametrize(('hard', 'maximum', 'target'), [(2000, 262144, 2000),
                          (10000, 1500, 1500), (-1, 1500, 1500)])
def test_hard_and_max_soft_clamps(guard, hard, maximum, target):
    resource, snapshot, _ = guard
    snapshot.return_value = (800, {})
    resource.getrlimit.side_effect = [(1000, hard), (target, hard)]
    check(settings={'max_soft': maximum})
    resource.setrlimit.assert_called_once_with(7, (target, hard))
    assert limits.fd_headroom_status()['state'] == 'raised'


@pytest.mark.parametrize('failure', ['capped', 'denied', 'unconfirmed'])
def test_capped_denied_or_unconfirmed_never_logs_raised(guard, caplog, failure):
    resource, snapshot, notify = guard
    snapshot.return_value = (800, {})
    if failure == 'capped':
        resource.getrlimit.return_value = (1000, 1000)
    if failure == 'denied':
        resource.setrlimit.side_effect = PermissionError('private denial')
    with caplog.at_level(logging.WARNING):
        check()
    assert limits.fd_headroom_status()['state'] == 'capped'
    assert 'fd-headroom: raised soft' not in caplog.text
    assert notify.call_count == 1


def test_public_status_filters_private_error(guard):
    _, snapshot, notify = guard
    snapshot.return_value = (100, {})
    check()
    limits._fd_status.update(error_message='/private/secret', argv=['card text'],
                              incident={'error': 'credential detail'})
    state = limits.fd_headroom_status()
    assert set(state) <= {'state', 'sampled_at', 'count', 'soft', 'hard', 'ratio',
                          'last_card_id', 'create_state', 'failing_since', 'error_class', 'skipped'}
    assert 'private' not in str(state) and 'credential' not in str(state)
    assert state['state'] == 'ok' and state['count'] == 100
    notify.assert_not_called()


@pytest.mark.parametrize(('key', 'value', 'default'), [
    ('enabled', 'false', True), ('warn_ratio', -1, .5), ('raise_ratio', 1, .75),
    ('horizon_hours', 0, 6), ('interval_seconds', 1, 300), ('max_soft', 4, 262144),
    ('horizon_hours', float('nan'), 6)])
def test_invalid_config_falls_back_and_marks_status(guard, caplog, key, value, default):
    from hermes_cli.fd_headroom_config import validated
    settings, errors = validated({'runtime': {'fd_headroom': {key: value}}})
    assert settings[key] == default and key in errors
    with caplog.at_level(logging.WARNING):
        check(settings={key: value})
    assert limits.fd_headroom_status()['state'] == 'config_error'


def test_interval_590_retains_four_sample_trend_and_591_falls_back(guard):
    _, snapshot, notify = guard
    for now, count in [(0, 400), (591, 440), (1182, 480), (1773, 520)]:
        snapshot.return_value = (count, {})
        sample = check(now, settings={'interval_seconds': 590, 'warn_ratio': .7})
    assert sample is not None and sample['slope'] == pytest.approx(120 * 60 / 1773)
    assert notify.call_count == 1
    check(2400, settings={'interval_seconds': 591})
    assert limits.fd_headroom_status()['state'] == 'config_error'
    assert limits._fd_interval == 300


def test_ratio_order_falls_back_to_valid_defaults(guard):
    from hermes_cli.fd_headroom_config import validated
    settings, errors = validated({'runtime': {'fd_headroom': {'warn_ratio': .9, 'raise_ratio': .8}}})
    assert 0 < settings['warn_ratio'] < settings['raise_ratio'] < 1 and errors
