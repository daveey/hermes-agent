"""Interim seals must not swallow an adapter's accepted stream boundary."""
from types import SimpleNamespace

import pytest

from gateway.config import Platform, StreamingConfig
from gateway.session_authority import SessionAuthority
from gateway.session_events import SessionEvents
from gateway.stream_consumer import StreamConsumerConfig
from tests.gateway.test_local_viewer_events_turn import _turn


class RecordingAdapter:
    SUPPORTS_MESSAGE_EDITING = True
    MAX_MESSAGE_LENGTH = 4096

    def __init__(self):
        self.messages = {}

    async def send(self, *, content, **kwargs):
        message_id = str(len(self.messages))
        self.messages[message_id] = content
        return SimpleNamespace(success=True, message_id=message_id)

    async def edit_message(self, *, message_id, content, **kwargs):
        self.messages[message_id] = content
        return SimpleNamespace(success=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('platform', [Platform.LOCAL, Platform.TELEGRAM])
@pytest.mark.parametrize('viewer', [False, True])
@pytest.mark.parametrize('already_streamed', [True, False])
async def test_interim_callback_preserves_consumer_segments(platform, viewer, already_streamed):
    live = SimpleNamespace(tool_progress_mode=None, event_stream=SessionEvents())
    frames = []
    if viewer:
        live.event_stream.observers.add(frames.append)
    turn = _turn(platform, [], live=live)
    authority = turn._approval_owner[0]
    authority.executing = {'s': 1}
    authority.publish_execution = lambda *args: SessionAuthority.publish_execution(authority, *args)
    # Exercise the real publisher, including its no-observer False result and replay.
    del turn._publish_execution
    adapter = RecordingAdapter()
    turn._runner.config = SimpleNamespace(streaming=StreamingConfig(enabled=True))
    turn._runner._delivery_adapter_for = lambda source: adapter
    turn._runner._build_stream_consumer_config = lambda *args, **kwargs: (
        StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1, cursor=''), None)
    turn._ctx.resolve_display_setting = lambda *args: True
    turn._ctx._status_adapter = None
    turn._ctx._status_thread_metadata = None
    turn._ctx.event_message_id = None
    turn._ctx.interim_assistant_messages_enabled = True
    consumer, delta, interim, _ = turn._setup_stream_consumer(platform.value)
    assert consumer is not None and consumer.stream_deltas_enabled
    if already_streamed:
        delta('First.')
    interim('First.', already_streamed=already_streamed)
    delta('Final.')
    consumer.finish()
    await consumer.run()
    expected = ['Final.'] if platform == Platform.LOCAL and not viewer and not already_streamed else ['First.', 'Final.']
    assert list(adapter.messages.values()) == expected
    replay = live.event_stream.since(live.event_stream.epoch, 0)['events']
    seals = [frame['payload'] for frame in replay if frame['type'] == 'message.interim']
    if platform == Platform.LOCAL:
        assert len(seals) == 1
        assert seals[0]['text'] == 'First.'
        assert seals[0]['already_streamed'] is already_streamed
    else:
        assert seals == []
