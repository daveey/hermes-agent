"""Ephemeral API listeners publish the address they actually own."""
import logging

import pytest
pytest.importorskip("aiohttp", reason="optional messaging extra")
import aiohttp

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.mark.asyncio
async def test_ephemeral_listener_reports_bound_port(caplog):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={
        'host': '127.0.0.1', 'port': 0, 'key': 'test-owned-strong-api-secret',
    }))
    try:
        with caplog.at_level(logging.INFO):
            assert await adapter.connect()
        port = adapter._runner.addresses[0][1]
        assert port > 0
        assert adapter._api_server_status_payload()['port'] == port
        assert f'API server listening on http://127.0.0.1:{port} ' in caplog.text
        async with aiohttp.ClientSession() as client:
            async with client.get(f'http://127.0.0.1:{port}/health') as response:
                assert response.status == 200
    finally:
        await adapter.disconnect()
