"""Local daemon probes use the RPC hang bound, not a shorter pong deadline."""
import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from tests.gateway.fixtures import local_recovery_probe as probe


@pytest.mark.parametrize('options,expected_timeout', [({}, None), ({'ping_timeout': 5}, 5)])
def test_probe_heartbeat_policy_and_explicit_override(tmp_path, monkeypatch, options, expected_timeout):
    monkeypatch.setattr(probe, 'control', lambda *args: {'ticket': 'test-ticket'})

    async def echo(ws):
        async for message in ws:
            request = json.loads(message)
            await ws.send(json.dumps({'id': request['id'], 'result': 'ok'}))

    async def run():
        async with serve(echo, '127.0.0.1', 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            desc = {'api_origin': f'http://127.0.0.1:{port}', 'instance_id': 'test'}
            async with probe.websocket(tmp_path, desc, **options) as ws:
                assert ws.ping_interval is not None
                assert ws.ping_timeout == expected_timeout
                assert (await probe.rpc(ws, 'test.read'))['result'] == 'ok'

    asyncio.run(run())


def test_unanswered_rpc_still_times_out_without_pong_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, 'control', lambda *args: {'ticket': 'test-ticket'})
    monkeypatch.setattr(probe, 'RPC_DEADLINE', 2)

    async def run():
        received = asyncio.Event()

        async def silent(ws):
            await ws.recv()
            received.set()
            await ws.wait_closed()

        async with serve(silent, '127.0.0.1', 0) as server:
            port = next(iter(server.sockets)).getsockname()[1]
            desc = {'api_origin': f'http://127.0.0.1:{port}', 'instance_id': 'test'}
            async with probe.websocket(tmp_path, desc) as ws:
                with pytest.raises(TimeoutError):
                    await probe.rpc(ws, 'test.unanswered')
                assert received.is_set()

    asyncio.run(run())
