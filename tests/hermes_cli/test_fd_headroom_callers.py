"""Execute existing service ticks, not text-only wiring assertions."""
import asyncio
import threading
from unittest.mock import Mock

import pytest
from hermes_cli import resource_limits as limits


def test_gateway_housekeeping_runs_check_and_publishes_status(monkeypatch, tmp_path):
    import gateway.run as gateway
    from gateway import status
    stop = threading.Event()
    seen = []
    monkeypatch.setattr(limits, 'check_fd_headroom', lambda **kw: seen.append(kw) or {'count': 50})
    monkeypatch.setattr(limits, '_fd_status', {'count': 50, 'soft': 100,
                        'incident': {'error': 'offline', 'failure_since': 10000}})
    monkeypatch.setattr(status, '_get_runtime_status_path', lambda: tmp_path / 'gateway_state.json')
    monkeypatch.setattr(status, '_emit_runtime_status_transition', lambda *a: None)
    def chore(label, fn, *args, **kwargs):
        if label == 'FD headroom':
            fn(*args, **kwargs)
            stop.set()
    monkeypatch.setattr(gateway, '_housekeeping_chore', chore)
    gateway._start_gateway_housekeeping(stop, interval=0)
    assert seen == [{'role': 'gateway'}]
    assert status.flush_runtime_status()
    saved = status.read_runtime_status(tmp_path / 'gateway_state.json')
    assert saved['fd_headroom']['count'] == 50
    assert saved['fd_headroom']['incident']['error'] == 'offline'


def test_backend_tick_runs_check_and_health_exposes_status(monkeypatch):
    from hermes_cli import web_server as backend
    calls = []
    monkeypatch.setattr(limits, 'check_fd_headroom', lambda **kw: calls.append(kw))
    monkeypatch.setattr(limits, '_fd_status', {'count': 50, 'soft': 100,
                        'incident': {'error': 'offline', 'failure_since': 10000}})
    async def stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(backend.asyncio, 'sleep', stop)
    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            await backend._dashboard_selftest_loop()
    asyncio.run(exercise())
    assert calls == [{'role': 'backend'}]
    assert backend.DASHBOARD_HEALTH.snapshot()['fd_headroom']['incident']['error'] == 'offline'
