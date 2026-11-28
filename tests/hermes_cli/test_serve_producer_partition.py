"""Partitioned relay: server-bound home claim, atomic revocation, fail-closed without a binding."""
import pytest

from hermes_cli.serve_producer_home import HomeBinding
from hermes_cli.serve_producer_runtime import ProjectionRelay, ResourceExhausted


class Channel:
    def __init__(self, result):
        self.result = result
        self.sent = []

    def send(self, request, request_id):
        self.sent.append(request)

    def receive(self):
        return self.result


def binding(tag='1'):
    return HomeBinding('/homes/a', 'life' + tag, 'auth' + tag)


def partitioned(tmp_path, **kw):
    return ProjectionRelay({'partitioned': True}, home=tmp_path, runtime=tmp_path / 'r', generation='g', **kw)


def events_result(seq=3):
    return {'verb': 'EVENTS', 'key': 'events', 'generation': 'g', 'seq': seq, 'events': [], 'reset': True,
            'snapshot': {'rev': seq}}


def test_unpartitioned_relay_never_stamps(tmp_path):
    relay = ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'r', generation='g')
    assert not relay.partitioned and relay.partition_identity is None
    payload = {'body': {'a': 1}, 'accepted': 0}
    assert relay._stamped(payload) is payload
    assert relay.key_for(payload) == relay.key_for({'body': {'a': 1}, 'accepted': 5})
    key = relay.submit(payload)
    assert relay.pending_count == 1 and key
    relay.unbind_partition()  # no-op: nothing bound, nothing revoked
    assert relay.pending_count == 1


def test_partitioned_relay_refuses_everything_until_bound(tmp_path):
    relay = partitioned(tmp_path)
    assert relay.partitioned and relay.partition_identity is None
    with pytest.raises(ResourceExhausted, match='no authorized home'):
        relay.submit({'body': {'a': 1}, 'accepted': 0})
    with pytest.raises(ResourceExhausted, match='no authorized home'):
        relay.key_for({'body': {'a': 1}})
    assert relay.pending_count == 0


def test_claim_is_server_stamped_and_part_of_the_key(tmp_path):
    relay = partitioned(tmp_path)
    one = binding('1')
    assert relay.bind_partition(one) is True
    assert relay.bind_partition(one) is False  # identical binding: nothing revoked
    forged = {'body': {'a': 1, '_partition': {'home': '/homes/other'}}, 'accepted': 0}
    stamped = relay._stamped(forged)
    assert stamped['body']['_partition'] == one.wire() and forged['body']['_partition'] == {'home': '/homes/other'}
    assert relay._stamped({'a': 2})['_partition'] == one.wire()  # bare body form
    key = relay.key_for(forged)
    assert key == relay.key_for({'body': {'a': 1}, 'accepted': 9})  # claim is overwritten, so the key matches
    relay.bind_partition(binding('2'))
    assert relay.key_for({'body': {'a': 1}}) != key  # another generation never shares a key/ACK


def test_bind_change_and_unbind_revoke_queue_outputs_and_cursor_atomically(tmp_path):
    relay = partitioned(tmp_path)
    relay.bind_partition(binding('1'))
    key = relay.submit({'body': {'a': 1}, 'accepted': 0})
    relay._outputs[key] = (b'old', 10 ** 12)
    relay._bytes = 5
    relay._event_cursor = 9
    before = relay._revision
    relay.bind_partition(binding('2'))
    assert (relay.pending_count, len(relay._outputs), relay._bytes, relay._event_cursor) == (0, 0, 0, None)
    assert relay._revision == before + 1 and relay.read(key) is None

    relay.submit({'body': {'a': 1}, 'accepted': 0})
    relay._outputs['x'] = (b'old', 10 ** 12)
    relay._event_cursor = 4
    revision = relay._revision
    assert relay.unbind_partition() is True
    assert (relay.pending_count, len(relay._outputs), relay._bytes, relay._event_cursor) == (0, 0, 0, None)
    assert relay._revision == revision + 1 and relay.partition_identity is None
    assert relay.unbind_partition() is False  # already unbound: no further revision bump
    assert relay._revision == revision + 1
    with pytest.raises(ResourceExhausted):
        relay.submit({'body': {'a': 1}, 'accepted': 0})


def attach(relay, result):
    seen = []
    relay.set_event_sink(seen.append)
    relay._channel = Channel(result)
    return seen


def test_event_poll_carries_partition_and_requires_a_binding(tmp_path):
    relay = partitioned(tmp_path)
    seen = attach(relay, events_result())
    relay._poll_events()  # nothing bound: no request leaves, nothing is delivered
    assert relay._channel.sent == [] and seen == []
    relay.bind_partition(binding('1'))
    relay._poll_events()
    assert relay._channel.sent[0]['partition'] == binding('1').wire() and relay._channel.sent[0]['after'] == -1
    assert seen == [{'rev': 3, 'reset': True}]


def test_unpartitioned_event_poll_sends_no_partition(tmp_path):
    relay = ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'r', generation='g')
    attach(relay, events_result())
    relay._poll_events()
    assert 'partition' not in relay._channel.sent[0]


def test_stale_partition_is_revoked_before_any_event_is_delivered(tmp_path):
    relay = partitioned(tmp_path)
    seen = attach(relay, events_result())
    relay.bind_partition(binding('1'))
    asked = []
    relay.partition_check = lambda identity: asked.append(identity) or False
    key = relay.submit({'body': {'a': 1}, 'accepted': 0})
    relay._poll_events()
    assert asked == [binding('1').identity]
    assert relay._channel.sent == [] and seen == []
    assert relay.partition_identity is None and relay.pending_count == 0 and relay.read(key) is None


def test_current_partition_check_allows_delivery(tmp_path):
    relay = partitioned(tmp_path)
    seen = attach(relay, events_result())
    relay.bind_partition(binding('1'))
    relay.partition_check = lambda identity: identity == binding('1').identity
    relay._poll_events()
    assert seen and relay.partition_identity == binding('1').identity


def test_binding_changed_while_a_poll_is_in_flight_delivers_nothing(tmp_path):
    relay = partitioned(tmp_path)
    seen = []
    relay.set_event_sink(seen.append)
    relay.bind_partition(binding('1'))

    class Racing(Channel):
        def receive(self):
            relay.bind_partition(binding('2'))  # roster change lands during the child's reply
            return self.result

    relay._channel = Racing(events_result())
    relay._poll_events()
    assert seen == [] and relay._event_cursor is None
