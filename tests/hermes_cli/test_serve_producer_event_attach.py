"""A newly attached event sink must establish a snapshot before any increments."""
from hermes_cli.serve_producer_runtime import ProjectionRelay
import pytest


class Channel:
    def __init__(self, result):
        self.result = result
        self.sent = []

    def send(self, request, request_id):
        self.sent.append(request)

    def receive(self):
        return self.result


@pytest.mark.parametrize('cursor,reset,after,expected', [
    (None, True, -1, [{'rev': 8, 'reset': True}]),
    (None, False, -1, [{'rev': 8, 'reset': True}]),
    (-1, False, -1, [{'rev': 8, 'reset': True}]),
    (7, True, 7, [{'rev': 8, 'reset': True}]),
    (7, False, 7, [{'rev': 8}]),
])
def test_event_snapshot_on_attach_and_reset(tmp_path, cursor, reset, after, expected):
    relay = ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'runtime', generation='g')
    seen = []
    relay.set_event_sink(seen.append)
    relay._event_cursor = cursor
    channel = Channel({'verb': 'EVENTS', 'key': 'events', 'generation': 'g', 'seq': 8,
                       'events': [[8, {'rev': 8}]], 'reset': reset, 'snapshot': {'rev': 8}})
    relay._channel = channel
    relay._poll_events()
    assert channel.sent[0]['after'] == after
    assert seen == expected
    assert relay._event_cursor == 8
    assert relay.event_resets == int('reset' in expected[0])
