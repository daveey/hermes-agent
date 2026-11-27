"""Terminal delivery history is not an active failure on a healthy process."""
import json
import logging
from collections import deque
from unittest.mock import Mock

import pytest

from hermes_cli import fd_headroom_incident as incidents
from hermes_cli import kanban_sync_client as sync
from hermes_cli import resource_limits as limits


@pytest.fixture
def checker(tmp_path, monkeypatch):
    import hermes_state_guard
    monkeypatch.setattr(hermes_state_guard, '_in_test_context', lambda: False)
    monkeypatch.setattr(incidents, 'get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(sync, 'load_identity', lambda: None)
    resource = Mock(RLIMIT_NOFILE=7, RLIM_INFINITY=-1)
    resource.getrlimit.return_value = (1000, 10000)
    monkeypatch.setattr(limits, '_resource', resource)
    monkeypatch.setattr(limits, '_fd_core_head', lambda: 'a' * 40)
    monkeypatch.setattr(limits, '_fd_incident', {})
    monkeypatch.setattr(limits, '_fd_status', {})
    monkeypatch.setattr(limits, '_fd_last_check', None)
    monkeypatch.setattr(limits, '_fd_samples', deque())
    monkeypatch.setattr(limits, '_fd_interval', 300)
    calls = []
    def wire(identity, method, route, body, budget):
        calls.append(method)
        return {'request': {'state': 'rejected', 'result': {'error': 'fixture refusal'}}}
    monkeypatch.setattr(sync, '_bounded_call', wire)
    def check(count, now, probe=False):
        monkeypatch.setattr(limits, '_notify_fd_incident',
                            lambda sample, **kwargs: incidents.report(sample, now=10000 + now, **kwargs))
        monkeypatch.setattr(limits, '_fd_snapshot', lambda soft: (count, {'classes': {'other': count}}))
        return limits.check_fd_headroom({}, role='gateway', now=now, wall_now=10000 + now,
                                        probe=probe, probe_board='fd-proof' if probe else None)
    return check, calls


def test_healthy_terminal_does_not_hide_current_storage_failure(checker, monkeypatch):
    check, calls = checker
    check(600, 0)
    def denied(*args):
        raise PermissionError('fixture denied')
    monkeypatch.setattr(incidents, '_save', denied)
    result = check(100, 300)
    assert result['count'] == 100
    status = limits.fd_headroom_status()
    assert status['state'] == 'ok' and status['create_state'] == 'failing'
    assert status['error_class'] == 'PermissionError'
    assert calls == ['GET']


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('probe', [False, True])
def test_healthy_terminal_history_is_quiet_and_ok(checker, monkeypatch, caplog, restart, probe):
    check, calls = checker
    with caplog.at_level(logging.WARNING):
        first = check(600, 0, probe)
        path = incidents._path(first, probe)
        frozen = json.loads(path.read_text())
        if restart:
            limits._fd_incident = {}
            limits._fd_status.clear()
            limits._fd_samples.clear()
            limits._fd_last_check = None
            monkeypatch.setattr(incidents, '_producer_token', None)
        caplog.clear()
        for tick in range(1, 37):
            sample = check(100, tick * 300, probe)
            assert sample['count'] == 100
            status = limits.fd_headroom_status()
            assert status['state'] == 'ok'
            assert status['create_state'] == 'ok'
            assert status['failing_since'] is None and status['error_class'] is None
        assert not caplog.records, 'healthy terminal history must not emit repeated warnings'
    current = json.loads(path.read_text())
    assert current['terminal'] == frozen['terminal']
    assert current['envelope'] == frozen['envelope']
    assert calls == ['GET'], 'healthy history must not read/repost the terminal request'


def test_triggered_terminal_still_reports_failure_without_request_retry(checker, caplog):
    check, calls = checker
    check(600, 0)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        sample = check(600, 3900)
    assert sample['triggered']
    assert limits.fd_headroom_status()['create_state'] == 'failing'
    assert limits.fd_headroom_status()['error_class'] == 'TerminalRequest'
    assert 'card create failing since' in caplog.text
    assert calls == ['GET']


@pytest.mark.parametrize('restart', [False, True])
@pytest.mark.parametrize('probe', [False, True])
def test_transient_save_error_clears_after_storage_recovers(checker, monkeypatch, caplog, restart, probe):
    check, calls = checker
    first = check(600, 0, probe)
    path = incidents._path(first, probe)
    frozen = json.loads(path.read_text())
    real_save = incidents._save
    failures = [1]
    def flaky(path, state):
        if failures[0]:
            failures[0] -= 1
            raise OSError(28, 'fixture transient full disk')
        return real_save(path, state)
    monkeypatch.setattr(incidents, '_save', flaky)
    check(100, 300, probe)
    failed = json.loads(path.read_text())
    assert failed['error_class'] == 'OSError'
    assert limits.fd_headroom_status()['create_state'] == 'failing'
    if restart:
        limits._fd_incident = {}
        limits._fd_status.clear()
        limits._fd_samples.clear()
        limits._fd_last_check = None
        monkeypatch.setattr(incidents, '_producer_token', None)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        for tick in range(2, 38):
            sample = check(100, tick * 300, probe)
            assert sample['count'] == 100
            status = limits.fd_headroom_status()
            assert status['create_state'] == 'ok'
            assert status['error_class'] is None and status['failing_since'] is None
    assert not caplog.records
    current = json.loads(path.read_text())
    assert current['terminal'] == frozen['terminal']
    assert current['envelope'] == frozen['envelope']
    assert current['error_class'] == 'TerminalRequest'
    assert current['error'] == frozen['error']
    assert current['failing_since'] == frozen['failing_since']
    assert calls == ['GET']
    monkeypatch.setattr(incidents, '_producer_token', None)
    with caplog.at_level(logging.WARNING):
        sample = check(600, 12000, probe)
    assert sample['triggered']
    assert limits.fd_headroom_status()['create_state'] == 'failing'
    assert limits.fd_headroom_status()['error_class'] == 'TerminalRequest'
    assert 'card create failing since' in caplog.text
    assert calls == ['GET'], 'recovered history must remain latched for an unqualified birth'


@pytest.mark.parametrize('probe', [False, True])
def test_unreadable_discovery_preserves_healthy_sample(checker, probe):
    check, calls = checker
    sample = dict(host=limits.socket.gethostname(), role='gateway')
    path = incidents._path(sample, probe)
    path.mkdir(parents=True)
    result = check(100, 0, probe)
    assert result is not None and result['count'] == 100
    status = limits.fd_headroom_status()
    assert status['state'] == 'ok' and status['count'] == 100
    assert status['error_class'] == 'IsADirectoryError'
    assert status['create_state'] == 'failing'
    assert calls == []
