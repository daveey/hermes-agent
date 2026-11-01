"""A cancelled stand-in response must not strand the real Discord SDK's next request.

The gateway cancels tool-progress delivery at turn cleanup. discord.py releases
its rate-limit bucket with an await, so cancellation after parsing the response
can skip that release. A header-less stand-in leaves no expiry for recovery.
"""
from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("route", ["channel", "typing"])
def test_cancelled_response_does_not_strand_next_request(route):
    # conftest installs a discord stub in the pytest process. A cold child uses
    # the real SDK, as the messaging gateway does, against the actual HTTP fake.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), route],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def _exercise(route_name):
    import discord.http

    from tests.fakes.platforms.discord_standin import TOKEN, DiscordStandin

    standin = DiscordStandin()
    standin.start()
    client = discord.http.HTTPClient(asyncio.get_running_loop())
    original = discord.http.json_or_text
    cancelled = False
    path = "/channels/{channel_id}" + ("/typing" if route_name == "typing" else "")
    method = "POST" if route_name == "typing" else "GET"

    async def cancel_after_body(response):
        nonlocal cancelled
        result = await original(response)
        if response.url.path.endswith("/channels/123" + ("/typing" if route_name == "typing" else "")) and not cancelled:
            cancelled = True
            task = asyncio.current_task()
            assert task is not None
            # Deliver cancellation at the SDK's next await: bucket release.
            asyncio.get_running_loop().call_soon(task.cancel)
        return result

    try:
        standin.add_text_channel("123", "test")
        discord.http.Route.BASE = standin.api_base
        # Route.url is materialized at construction, after BASE is redirected.
        route = discord.http.Route(method, path, channel_id=123)
        await client.static_login(TOKEN)
        discord.http.json_or_text = cancel_after_body
        first = asyncio.create_task(client.request(route))
        with contextlib.suppress(asyncio.CancelledError):
            await first
        assert cancelled and first.cancelled(), "cancellation boundary was not exercised"
        second = await asyncio.wait_for(client.request(route), timeout=5)
        if route_name == "channel":
            assert second["id"] == "123"
        else:
            assert second == ""
    finally:
        discord.http.json_or_text = original
        await client.close()
        standin.stop()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    asyncio.run(_exercise(sys.argv[1]))
