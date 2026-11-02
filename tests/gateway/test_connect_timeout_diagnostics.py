"""A connection deadline must preserve the blocked await, without credential locals."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.run_adapters import GatewayAdapterLifecycleMixin


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [True, False])
async def test_deadline_records_connect_and_bot_wait_without_secrets(initial, caplog):
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def stalled_handshake(token):
        entered.set()
        await blocked.wait()

    class Adapter:
        _bot_task = None

        async def connect(self, *, is_reconnect):
            self._bot_task = asyncio.create_task(stalled_handshake("DO-NOT-LOG-TOKEN"))
            await entered.wait()
            await blocked.wait()

    class Runner(GatewayAdapterLifecycleMixin):
        def _platform_connect_timeout_secs(self, platform=None, *, initial=False):
            return 0.01

        @staticmethod
        async def _wait_or_detach(task, timeout):
            await asyncio.wait_for(entered.wait(), 5)
            return await GatewayAdapterLifecycleMixin._wait_or_detach(task, timeout)

    adapter = Adapter()
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        try:
            with pytest.raises(TimeoutError, match="discord connect timed out"):
                await Runner()._connect_adapter_bounded(
                    adapter, Platform.DISCORD, is_reconnect=not initial, initial=initial,
                )
            assert "Adapter.connect" in caplog.text
            assert "stalled_handshake" in caplog.text
            assert "Event.wait" in caplog.text
            assert "DO-NOT-LOG-TOKEN" not in caplog.text
        finally:
            if adapter._bot_task:
                adapter._bot_task.cancel()
                await asyncio.gather(adapter._bot_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_success_has_no_timeout_diagnostic(caplog):
    async def connect(**kwargs):
        return True

    class Runner(GatewayAdapterLifecycleMixin):
        def _platform_connect_timeout_secs(self, platform=None, *, initial=False):
            return 2

    assert await Runner()._connect_adapter_bounded(
        SimpleNamespace(connect=connect), Platform.DISCORD, is_reconnect=False, initial=True,
    )
    assert "Platform connect deadline" not in caplog.text
