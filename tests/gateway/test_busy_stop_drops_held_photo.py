"""A busy ``/stop`` or ``/new`` drops the photo burst the Telegram adapter still holds (#81370).

The idle path drops held input once the command passes the slash-access gate and command hooks
(``_hm_drop_held_input``); the busy fast-path (``_busy_stop_command`` / ``_busy_new_command``) must
do the same once past its own access gate, or the old chat's photo flushes as a turn after the
stop. A refused command leaves the buffer alone. Real GatewayRunner busy dispatch, real
TelegramAdapter photo batching and a real authority over SQLite; the running turn is a stand-in so
the busy state is deterministic.
"""
import asyncio

import pytest


async def _rig(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session_authority import SessionAuthority, initialize_session_authority
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.setattr(SessionAuthority, '_schedule', lambda self, ref: None)
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '42')
    runner = GatewayRunner(GatewayConfig())
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token='fixture-token'))
    adapter._media_batch_delay_seconds = 600.0  # the flush never fires inside the test on its own
    runner.adapters = {Platform.TELEGRAM: adapter}
    authority = await initialize_session_authority(runner, profile_id='default', instance_id='owner')
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id='42', chat_type='dm', user_id='42')
    photo = MessageEvent(text='old photo caption', message_type=MessageType.PHOTO, source=source,
                         message_id='P', media_urls=['/tmp/p1.png'], media_types=['image/png'])
    key = adapter._event_session_key(photo)
    adapter._enqueue_photo_event(f'{key}:photo-burst', photo)
    assert adapter._pending_photo_batches, 'the photo is held in the burst buffer'

    class RunningAgent:
        def interrupt(self, message=None):
            pass

    runner._session_state(runner.session_store._generate_session_key(source)).turn.agent = RunningAgent()
    return runner, adapter, authority, source


@pytest.mark.asyncio
@pytest.mark.parametrize('command,dropped,refused', [('/stop', True, False), ('/new', True, False), ('/reset', True, False),
                                                     ('/status', False, False), ('/stop', False, True)])
async def test_busy_command_settles_the_held_photo(tmp_path, monkeypatch, command, dropped, refused):
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.event import MessageEvent

    runner, adapter, authority, source = await _rig(monkeypatch, tmp_path)
    if refused:
        runner.config.platforms[Platform.TELEGRAM] = PlatformConfig(
            enabled=True, extra={'allow_admin_from': ['99'], 'user_allowed_commands': ['status']})
        assert runner._check_slash_access(source, command.lstrip('/')) is not None
    tasks = dict(adapter._pending_photo_batch_tasks)
    try:
        reply = await runner._handle_message(MessageEvent(text=command, source=source))
        assert reply is not None
        if dropped:
            assert adapter._pending_photo_batches == {}
            assert adapter._pending_photo_batch_tasks == {}
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            assert tasks and all(t.cancelled() for t in tasks.values()), 'the flush was cancelled, not run'
        else:
            assert len(adapter._pending_photo_batches) == 1, 'a refused or non-reset command keeps the photo'
            assert not any(t.done() for t in tasks.values())
    finally:
        for t in adapter._pending_photo_batch_tasks.values():
            t.cancel()
        await asyncio.to_thread(authority.db.close)
