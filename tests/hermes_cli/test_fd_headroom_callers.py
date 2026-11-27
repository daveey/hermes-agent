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


def test_gateway_thread_publishes_public_status(monkeypatch, tmp_path):
    from gateway import status
    monkeypatch.setattr(limits, 'check_fd_headroom', lambda **kw: {'count': 50})
    monkeypatch.setattr(limits, '_fd_status', {'count': 50, 'create_state': 'failing',
                        'error_class': 'OSError', 'failing_since': 10000,
                        'incident': {'error': '/private/path'}})
    monkeypatch.setattr(status, '_get_runtime_status_path', lambda: tmp_path / 'gateway_state.json')
    monkeypatch.setattr(status, '_emit_runtime_status_transition', lambda *a: None)
    limits.gateway_fd_headroom_tick()
    assert status.flush_runtime_status()
    saved = status.read_runtime_status(tmp_path / 'gateway_state.json')['fd_headroom']
    assert saved['count'] == 50 and saved['create_state'] == 'failing'
    assert 'private' not in str(saved)
