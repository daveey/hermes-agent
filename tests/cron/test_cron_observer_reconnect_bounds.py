"""Typed/bounded recovery controls with a deterministic clock, never a real outage wait."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from cron import observer_reconnect as recovery
from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError, GatewayUnavailableError


@pytest.mark.parametrize('error,attempted,retries', [
    (GatewayClientError('lost'), True, True),
    (GatewayClientError('lost'), False, False),
    (GatewayRPCError('permission_denied'), True, False),
    (GatewayUnavailableError('down', 'absent'), True, True),
    (GatewayUnavailableError('starting', 'starting'), True, True),
    (GatewayUnavailableError('draining', 'draining'), True, True),
    (GatewayUnavailableError('failed', 'failed'), True, False),
])
def test_transport_retry_is_bounded_and_refusals_fail_closed(monkeypatch, error, attempted, retries):
    clock, connections, sleeps = [0.0], [], []

    @asynccontextmanager
    async def connect():
        connections.append(clock[0])
        raise error
        yield

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        assert 0 < seconds and clock[0] <= 3, 'reconnect exceeded its retry window'

    async def observe(call):
        pytest.fail('no connection should reach observation')

    async def probe():
        monkeypatch.setattr(recovery.asyncio, 'get_running_loop', lambda: SimpleNamespace(time=lambda: clock[0]))
        monkeypatch.setattr(recovery.asyncio, 'sleep', sleep)
        monkeypatch.setattr(recovery, 'RECONNECT_SECONDS', 3)
        with pytest.raises(type(error)) as caught:
            await recovery.observe_remote(connect, observe, lambda: attempted)
        assert caught.value is error

    asyncio.run(probe())
    assert connections == ([0, 2, 3] if retries else [0])
    assert sleeps == ([2, 1] if retries else [])


def test_recovery_ack_does_not_reset_failed_status_budget(monkeypatch):
    clock = [0.0]
    calls = []

    class Peer:
        async def rpc(self, method, **data):
            calls.append(method)
            if method == 'cron.status':
                raise GatewayClientError('lost')
            return {}

    @asynccontextmanager
    async def connect():
        yield Peer()

    async def observe(call):
        await call('recover', {})
        return await call('status', {})

    async def sleep(seconds):
        clock[0] += seconds
        assert 0 < seconds and clock[0] <= 3, 'successful recovery ACK reset the failed status budget'

    async def probe():
        monkeypatch.setattr(recovery.asyncio, 'get_running_loop', lambda: SimpleNamespace(time=lambda: clock[0]))
        monkeypatch.setattr(recovery.asyncio, 'sleep', sleep)
        monkeypatch.setattr(recovery, 'RECONNECT_SECONDS', 3)
        with pytest.raises(GatewayClientError):
            await recovery.observe_remote(connect, observe, lambda: True)

    asyncio.run(probe())
    assert calls == ['cron.recover', 'cron.status'] * 3


def test_successful_status_starts_a_fresh_outage_budget(monkeypatch):
    clock, connections = [0.0], []

    class Peer:
        def __init__(self):
            self.calls = 0

        async def rpc(self, method, **data):
            self.calls += 1
            if self.calls == 2:
                raise GatewayClientError('next outage')
            clock[0] += 3
            return {'status': 'started'}

    @asynccontextmanager
    async def connect():
        peer = Peer()
        connections.append(peer)
        yield peer

    async def observe(call):
        result = await call('status', {})
        if len(connections) < 3:
            await call('status', {})
        return result

    async def sleep(seconds):
        clock[0] += seconds

    async def probe():
        monkeypatch.setattr(recovery.asyncio, 'get_running_loop', lambda: SimpleNamespace(time=lambda: clock[0]))
        monkeypatch.setattr(recovery.asyncio, 'sleep', sleep)
        monkeypatch.setattr(recovery, 'RECONNECT_SECONDS', 3)
        assert await recovery.observe_remote(connect, observe, lambda: True) == {'status': 'started'}

    asyncio.run(probe())
    assert len(connections) == 3
