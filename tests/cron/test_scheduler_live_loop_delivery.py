"""cron._deliver_result against a real gateway event loop on a thread: live-send timeout and
partial split delivery (moved from test_scheduler.py)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from cron.scheduler import _deliver_result


class TestDeliverResultTimeoutCancelsFuture:
    """When the live adapter's confirmation outlasts the wait, the outcome depends on whether the
    send had STARTED on the gateway loop. Started: it is in flight (a paced multi-chunk send can
    legitimately outlast the wait) — leave it running and skip the standalone fallback, which would
    duplicate it (#38922). Never started (wedged loop): nothing was sent, so fall through to
    standalone or the message is silently dropped. ``future.cancel()`` cannot tell the two apart:
    a run_coroutine_threadsafe future stays PENDING until done, so cancel() returns True mid-send
    and kills it — these tests drive a real loop for that reason.
    """

    def _deliver(self, monkeypatch, adapter, loop):
        from cron import scheduler_delivery
        from gateway.config import Platform

        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
        monkeypatch.setattr(scheduler_delivery, "_LIVE_SEND_CONFIRM_TIMEOUT_SECS", 0.3)
        job = {"id": "timeout-job", "deliver": "origin", "origin": {"platform": "telegram", "chat_id": "123"}}
        standalone_send = AsyncMock(return_value={"success": True})
        with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
             patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
             patch("tools.send_message_tool._send_to_platform", new=standalone_send):
            result = _deliver_result(job, "Hello world", adapters={Platform.TELEGRAM: adapter}, loop=loop)
        return result, standalone_send

    def test_in_flight_send_outlasting_the_wait_keeps_running_and_is_not_duplicated(self, monkeypatch):
        import asyncio
        import time

        loop = _start_running_loop()
        events = []

        async def slow_send(chat_id, content, **_kw):
            events.append("started")
            await asyncio.sleep(0.6)  # outlasts the 0.3s confirmation wait
            events.append("finished")
            return MagicMock(success=True, message_id="m1", raw_response=None)

        adapter = MagicMock()
        adapter.send = slow_send
        try:
            result, standalone_send = self._deliver(monkeypatch, adapter, loop)
            time.sleep(0.6)
        finally:
            loop.call_soon_threadsafe(loop.stop)
        assert result is None, f"expected the in-flight send to count as delivered, got {result!r}"
        standalone_send.assert_not_awaited()
        assert events == ["started", "finished"], "the in-flight send must not be cancelled mid-way"

    def test_send_that_never_started_falls_back_to_standalone(self, monkeypatch):
        import time

        loop = _start_running_loop()
        loop.call_soon_threadsafe(time.sleep, 0.8)  # wedge the running loop past the 0.3s wait
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True))
        try:
            result, standalone_send = self._deliver(monkeypatch, adapter, loop)
            time.sleep(0.8)  # the loop un-wedges: the abandoned send must still never go out
        finally:
            loop.call_soon_threadsafe(loop.stop)
        assert result is None, f"standalone should have delivered, got {result!r}"
        standalone_send.assert_awaited_once()
        adapter.send.assert_not_awaited()


def _start_running_loop():
    """Start a fresh event loop on a daemon thread and return it once ``loop.is_running()`` holds.

    ``_deliver_result`` takes the live-adapter lane only when the loop is already running; a bare
    ``Thread(target=loop.run_forever).start()`` returns before that, so a descheduled loop thread
    sent the test down the standalone lane on a CPU-throttled runner.
    """
    import threading

    loop = asyncio.new_event_loop()
    running = threading.Event()
    loop.call_soon(running.set)  # the first callback run_forever runs, after is_running() flips
    threading.Thread(target=loop.run_forever, daemon=True).start()
    assert running.wait(timeout=30), "event loop thread never started running"
    return loop


class TestDeliverResultPartialSplitDelivery:
    def test_partial_split_delivery_is_not_resent_by_standalone(self):
        """A split send that failed after delivering its head must not fall through to standalone,
        which would deliver the whole payload again; the run reports the partial failure instead."""
        from gateway.config import Platform
        from gateway.platforms.base import SendResult

        loop = _start_running_loop()
        adapter = MagicMock()
        adapter.splits_long_messages = True
        adapter.send = AsyncMock(return_value=SendResult(
            success=False, error="Twilio 400: rejected",
            raw_response={"partial_overflow": True, "delivered_chunks": 2, "total_chunks": 5}))
        pconfig = MagicMock()
        pconfig.enabled = True
        mock_cfg = MagicMock()
        mock_cfg.platforms = {Platform.TELEGRAM: pconfig}
        job = {"id": "partial-job", "deliver": "origin", "origin": {"platform": "telegram", "chat_id": "123"}}
        standalone_send = AsyncMock(return_value={"success": True})
        try:
            with patch("gateway.config.load_gateway_config", return_value=mock_cfg), \
                 patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
                 patch("tools.send_message_tool._send_to_platform", new=standalone_send):
                result = _deliver_result(job, "Hello world", adapters={Platform.TELEGRAM: adapter}, loop=loop)
        finally:
            loop.call_soon_threadsafe(loop.stop)
        adapter.send.assert_awaited_once()
        standalone_send.assert_not_awaited()
        assert result and "delivered 2 of 5 chunks" in result, result
