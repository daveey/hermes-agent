"""A websocket that closes while uvicorn's keepalive awaits a pong is not a loop error.

websockets' legacy protocol returns ``asyncio.shield(pong_waiter)`` from ``ping()``. When the
connection closes, ``close_connection`` cancels the keepalive task (cancelling the shield's
outer future) and ``abort_pings`` then sets ``ConnectionClosed`` on the inner waiter. asyncio's
shield logs that as "ConnectionClosedOK exception in shielded future" at ERROR — on every Desktop
SSH-tunnel disconnect, where the serve backend runs keepalive pings (Baby Bear mini, BB-70/71/75).
"""

from __future__ import annotations

import asyncio

import pytest

from tui_gateway.loop_noise import install_loop_noise_filter

websockets_exceptions = pytest.importorskip("websockets.exceptions")
websockets_frames = pytest.importorskip("websockets.frames")


def _closed_ok():
    close = websockets_frames.Close(websockets_frames.CloseCode.NO_STATUS_RCVD, "")
    return websockets_exceptions.ConnectionClosedOK(close, close, True)


def _shielded_failure(loop: asyncio.AbstractEventLoop, exc: BaseException) -> None:
    """Replay the keepalive sequence: await a shielded waiter, cancel the awaiter, then fail the waiter."""
    async def scenario():
        waiter = loop.create_future()

        async def keepalive():
            await asyncio.shield(waiter)

        task = asyncio.ensure_future(keepalive())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        waiter.set_exception(exc)
        waiter.cancel()  # abort_pings' no-op cancel on the already-done waiter
        await asyncio.sleep(0)

    loop.run_until_complete(scenario())


@pytest.fixture
def loop_with_capture():
    loop = asyncio.new_event_loop()
    forwarded: list[dict] = []
    loop.set_exception_handler(lambda _loop, ctx: forwarded.append(ctx))
    install_loop_noise_filter(loop)
    try:
        yield loop, forwarded
    finally:
        loop.close()


def test_closed_websocket_in_shielded_keepalive_is_not_forwarded(loop_with_capture):
    loop, forwarded = loop_with_capture
    _shielded_failure(loop, _closed_ok())
    assert forwarded == []


def test_other_exception_in_shielded_future_still_forwarded(loop_with_capture):
    loop, forwarded = loop_with_capture
    _shielded_failure(loop, RuntimeError("real bug behind a shield"))
    assert len(forwarded) == 1
    assert isinstance(forwarded[0]["exception"], RuntimeError)


def test_closed_websocket_outside_a_shield_still_forwarded(loop_with_capture):
    loop, forwarded = loop_with_capture
    ctx = {"message": "Task exception was never retrieved", "exception": _closed_ok()}
    loop.call_exception_handler(ctx)
    assert forwarded == [ctx]
