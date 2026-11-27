"""Run actual service ticks: immediate off-loop work and no overlapping queued checks."""
import asyncio
import threading
from unittest.mock import Mock

import pytest
from hermes_cli import resource_limits as limits


def test_requested_tick_one_worker_and_overlap_skip(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def check(**kwargs):
        calls.append((threading.get_ident(), kwargs))
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(limits, 'check_fd_headroom', check)
    monkeypatch.setattr(limits, '_fd_status', {})
    try:
        limits.request_fd_headroom(role='backend')
        assert entered.wait(3)
        limits.request_fd_headroom(role='backend')
        assert len(calls) == 1
        assert calls[0][0] != threading.get_ident()
        assert limits.fd_headroom_status()['skipped'] == 1
    finally:
        release.set()
        limits._fd_thread.join(3)


def test_gateway_housekeeping_first_action_requests_sample(monkeypatch):
    import gateway.run as gateway
    stop = threading.Event()
    calls = []
    def request(**kw):
        calls.append(kw)
        stop.set()
    monkeypatch.setattr(limits, 'request_fd_headroom', request)
    chores = Mock()
    monkeypatch.setattr(gateway, '_housekeeping_chore', chores)
    gateway._start_gateway_housekeeping(stop, interval=60)
    assert calls == [{'role': 'gateway'}]
    chores.assert_not_called()  # No slow directory/cron/census preceding the first request.


def test_backend_first_tick_dispatches_without_waiting_for_sample(monkeypatch):
    from hermes_cli import web_server as backend
    calls = []
    monkeypatch.setattr(limits, 'request_fd_headroom', lambda **kw: calls.append(kw))
    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(backend.asyncio, 'sleep', stop)
    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            await backend._dashboard_selftest_loop()
    asyncio.run(exercise())
    assert calls == [{'role': 'backend'}]


def test_backend_short_interval_preserves_selftest_cadence(monkeypatch):
    from hermes_cli import web_server as backend
    clock, samples, selftests = [0.0], [], []
    monkeypatch.setattr(backend.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(limits, 'fd_headroom_delay', lambda maximum: min(30, maximum), raising=False)
    monkeypatch.setattr(limits, 'request_fd_headroom', lambda **kw: samples.append(clock[0]))
    async def sleep(seconds):
        clock[0] += seconds
        if clock[0] > 90:
            raise asyncio.CancelledError
    async def selftest():
        selftests.append(clock[0])
    monkeypatch.setattr(backend.asyncio, 'sleep', sleep)
    monkeypatch.setattr(backend, '_dashboard_selftest_once', selftest)
    monkeypatch.setattr(backend.app.state, 'auth_required', False, raising=False)
    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            await backend._dashboard_selftest_loop()
    asyncio.run(exercise())
    assert samples == [0, 30, 60, 90]
    assert selftests == [60]


def test_gateway_short_interval_does_not_accelerate_chores(monkeypatch):
    import gateway.run as gateway
    import gateway.run_delivery_queue_watch as watch
    clock, samples, chores = [0.0], [], []
    stop = threading.Event()
    monkeypatch.setattr(gateway.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(limits, 'fd_headroom_delay', lambda maximum: min(30, maximum), raising=False)
    monkeypatch.setattr(limits, 'request_fd_headroom', lambda **kw: samples.append(clock[0]))
    monkeypatch.setattr(gateway, '_housekeeping_chore', lambda *a: chores.append(clock[0]))
    def wait(*args):
        clock[0] += args[1]
        if clock[0] >= 90:
            stop.set()
    monkeypatch.setattr(watch, 'wait_for_next_tick', wait)
    gateway._start_gateway_housekeeping(stop, interval=60)
    assert sorted(set(samples)) == [0, 30, 60]
    assert sorted(set(chores)) == [0, 60]


def test_gateway_boot_dispatches_before_platform_initialization(monkeypatch):
    import gateway.code_skew as skew
    import gateway.run as gateway
    calls = []
    class StopStartup(Exception):
        pass
    monkeypatch.setattr(limits, 'apply_nofile_soft_limit', lambda: calls.append('floor'))
    monkeypatch.setattr(skew, 'record_boot_fingerprint', lambda: None)
    monkeypatch.setattr(skew, 'install_boot_pinned_imports', lambda: None)
    def request(**kw):
        calls.append(kw)
        raise StopStartup
    monkeypatch.setattr(limits, 'request_fd_headroom', request)
    async def exercise():
        with pytest.raises(StopStartup):
            await gateway.start_gateway()
    asyncio.run(exercise())
    assert calls == ['floor', {'role': 'gateway'}]


def test_serve_boot_dispatches_before_server(monkeypatch):
    from tests.hermes_cli import test_resource_limits as regression
    calls = []
    monkeypatch.setattr(limits, 'request_fd_headroom', lambda **kw: calls.append(kw))
    regression.test_serve_startup_applies_limit_before_web_server(monkeypatch)
    assert calls == [{'role': 'backend'}]


def test_dashboard_health_snapshot_excludes_private_fields(monkeypatch):
    from hermes_cli.web_server import DashboardHealth
    monkeypatch.setattr(limits, '_fd_status', {'state': 'warn', 'count': 50,
                        'error_class': 'TimeoutError', 'error': '/private/token',
                        'envelope': {'body': 'private card text'}})
    public = DashboardHealth().snapshot()['fd_headroom']
    assert public == {'state': 'warn', 'count': 50, 'error_class': 'TimeoutError'}


def test_delay_uses_cached_worker_deadline(monkeypatch):
    monkeypatch.setattr(limits, '_fd_interval', 30)
    monkeypatch.setattr(limits, '_fd_last_check', 100)
    monkeypatch.setattr(limits.time, 'monotonic', lambda: 110)
    assert limits.fd_headroom_delay(60) == 20
    assert limits.fd_headroom_delay(5) == 5
    monkeypatch.setattr(limits.time, 'monotonic', lambda: 131)
    assert limits.fd_headroom_delay(60) == 1


@pytest.mark.parametrize('role', ['backend', 'gateway'])
@pytest.mark.parametrize('failure', ['unsupported', 'load', 'backend'])
def test_unsampled_service_ticks_keep_bounded_deadline(monkeypatch, role, failure, caplog):
    from hermes_cli import config
    from hermes_cli.config_backend import ConfigBackendUnavailable
    clock, starts = [0.0], []
    monkeypatch.setattr(limits.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(limits, '_fd_last_check', None)
    monkeypatch.setattr(limits, '_fd_interval', 300)
    monkeypatch.setattr(limits, '_fd_status', {})
    monkeypatch.setattr(limits, '_fd_thread', None)
    monkeypatch.setattr(limits, '_fd_check_warning', None)
    if failure == 'unsupported':
        monkeypatch.setattr(limits, '_resource', None)
    else:
        def unavailable():
            if failure == 'backend':
                raise ConfigBackendUnavailable('unavailable')
            raise OSError('private config failure')
        monkeypatch.setattr(config, 'load_config_readonly', unavailable)
    class InlineThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args
        def start(self):
            starts.append(clock[0])
            self.target(*self.args)
        def is_alive(self):
            return False
    monkeypatch.setattr(limits.threading, 'Thread', InlineThread)
    if role == 'gateway':
        import gateway.run as gateway
        import gateway.run_delivery_queue_watch as watch
        stop = threading.Event()
        monkeypatch.setattr(gateway, '_housekeeping_chore', lambda *a: None)
        def wait(event, delay, *args, **kwargs):
            clock[0] += delay
            if clock[0] >= 60:
                stop.set()
        monkeypatch.setattr(watch, 'wait_for_next_tick', wait)
        gateway._start_gateway_housekeeping(stop, interval=60)
    else:
        from hermes_cli import web_server as backend
        async def sleep(delay):
            clock[0] += delay
            if clock[0] >= 60:
                raise asyncio.CancelledError
        monkeypatch.setattr(backend.asyncio, 'sleep', sleep)
        async def exercise():
            with pytest.raises(asyncio.CancelledError):
                await backend._dashboard_selftest_loop()
        asyncio.run(exercise())
    assert len(starts) <= 2, starts
    assert caplog.text.count('fd-headroom: check failed') <= 1
    assert limits.fd_headroom_delay(60) == 60


def test_gateway_thread_publishes_public_status(monkeypatch, tmp_path):
    from gateway import status
    monkeypatch.setattr(limits, 'check_fd_headroom', lambda **kw: {'count': 50})
    monkeypatch.setattr(limits, '_fd_status', {'count': 50, 'create_state': 'failing',
                        'error_class': 'OSError', 'failing_since': 10000,
                        'incident': {'error': '/private/path'}})
    monkeypatch.setattr(status, '_get_runtime_status_path', lambda: tmp_path / 'gateway_state.json')
    monkeypatch.setattr(status, '_emit_runtime_status_transition', lambda *a: None)
    assert status.acquire_gateway_runtime_lock()
    try:
        limits.gateway_fd_headroom_tick()
        assert status.flush_runtime_status()
    finally:
        status.release_gateway_runtime_lock()
    saved = status.read_runtime_status(tmp_path / 'gateway_state.json')['fd_headroom']
    assert saved['count'] == 50 and saved['create_state'] == 'failing'
    assert 'private' not in str(saved)
