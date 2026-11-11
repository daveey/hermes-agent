"""Chat ``/update`` must not count its own command as active work.

``hermes update`` asks each gateway to pause only when idle (no opt-in from a chat). A command that
registered itself as a turn would make that pause refuse every ``/update``.

Real GatewayRunner, DiscordAdapter and SessionAuthority; only the handler body is a stand-in.
"""
import pytest

from tests.gateway.test_messaging_slash_command_fifo import _owner
from tests.waiting import async_wait_until


@pytest.mark.asyncio
async def test_update_command_from_an_idle_chat_sees_no_active_work(monkeypatch):
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session_authority import initialize_session_authority

    runner, adapter, _sent = _owner(monkeypatch)
    await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id="42", chat_type="dm", user_id="42")
    seen = []

    async def update(event):
        seen.append(runner._active_work_count())  # what the updater's idle-only pause would read
        return "update started"
    monkeypatch.setattr(runner, "_handle_update_command", update)

    await adapter.handle_message(MessageEvent(text="/update", message_type=MessageType.COMMAND,
                                              source=source, message_id="U"))
    await async_wait_until(lambda: seen, what="/update to reach its handler")

    assert seen == [0]
    await async_wait_until(lambda: runner._active_work_count() == 0, what="the command to settle")
