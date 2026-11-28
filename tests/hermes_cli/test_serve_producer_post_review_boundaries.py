"""Failure/refusal controls for post-review lifecycle repair boundaries."""
import subprocess
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import serve_producer_admission as admission
from hermes_cli import serve_producer_custody as custody
from hermes_cli import serve_producer_runtime as runtime
from hermes_cli.serve_producer_protocol import ProtocolError
from tests.hermes_cli.test_serve_producer_broker_boundaries import Schedule
from tests.hermes_cli.test_serve_producer_event_attach import Channel
from tests.hermes_cli.test_serve_producer_merged_review import relay_at
from tests.hermes_cli.test_serve_producer_partition import binding
from tests.waiting import wait_until


def test_identical_rebind_fences_a_preceding_rebind_delivery(tmp_path):
    relay = relay_at(tmp_path)
    entered, release, seen = threading.Event(), threading.Event(), []

    def sink(payload):
        seen.append(payload)
        entered.set()
        assert release.wait(10)

    relay.set_event_sink(sink)
    relay._event_cursor = 0
    relay._channel = Channel({'verb': 'EVENTS', 'key': 'events', 'generation': 'g', 'seq': 1,
                              'events': [[1, {'n': 1}]]})
    poller = threading.Thread(target=relay._poll_events)
    poller.start()
    first = threading.Thread(target=relay.bind_partition, args=(binding('2'),))
    second = threading.Thread(target=relay.bind_partition, args=(binding('2'),))
    try:
        assert entered.wait(10)
        first.start()
        assert wait_until(lambda: relay.partition_identity == binding('2').identity, timeout=10)
        second.start()
        second.join(.2)
        assert second.is_alive(), 'identical bind returned before old sink completed'
    finally:
        release.set()
        poller.join(10)
        if first.ident is not None:
            first.join(10)
        if second.ident is not None:
            second.join(10)
    assert not poller.is_alive() and not first.is_alive() and not second.is_alive()


def test_sink_replacement_during_receive_preserves_new_attachment_cursor(tmp_path):
    relay = relay_at(tmp_path)
    seen = []
    relay.set_event_sink(seen.append)
    relay._event_cursor = 1
    channel = Channel({'verb': 'EVENTS', 'key': 'events', 'generation': 'g', 'seq': 2,
                       'events': [[2, {'n': 2}]]})
    receive = channel.receive

    def replace():
        relay.set_event_sink(seen.append)
        return receive()

    channel.receive = replace
    relay._channel = channel
    relay._poll_events()
    assert relay._event_cursor is None
    assert seen == []


@pytest.mark.parametrize('live', [False, True])
def test_no_owner_close_waits_for_the_spawned_process_without_signalling(tmp_path, live):
    relay = relay_at(tmp_path)
    waits = []

    def wait(timeout):
        waits.append(timeout)
        if live:
            raise subprocess.TimeoutExpired('owned', timeout)

    relay._spawned = SimpleNamespace(wait=wait)
    assert relay.close(timeout=0) is (not live)
    assert waits == [0]


def test_refused_spawn_timeout_is_retained_and_not_killed(tmp_path, monkeypatch):
    relay = relay_at(tmp_path)
    channel = SimpleNamespace(sock=SimpleNamespace(settimeout=lambda value: None),
                              receive=lambda: {'verb': 'REFUSED'}, close=lambda: None)
    waits = []

    def wait(timeout):
        waits.append(timeout)
        raise subprocess.TimeoutExpired('owned', timeout)

    process = SimpleNamespace(wait=wait)
    monkeypatch.setattr(runtime, 'spawn', lambda *args, **kwargs: (process, channel))
    with pytest.raises(ProtocolError, match='refused'):
        relay._spawn_owner(0)
    assert relay.close(timeout=0) is False
    assert waits == [0, 0]


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('error', [custody.CustodyConflict, custody.CustodyUnsafe])
def test_restart_gate_refusal_drains_without_replacement(tmp_path, monkeypatch, error):
    schedule = Schedule(monkeypatch, tmp_path, failed=True,
                        requests=[{'verb': 'PRODUCE', 'key': 'k', 'payload': {}}])

    def refuse(runtime):
        raise error('fixture refuses replacement gate')

    monkeypatch.setattr(admission.LaunchGate, 'acquire', refuse)
    schedule.run()
    assert schedule.stop_value
    assert schedule.spawns == 1
    assert schedule.sent == []
    assert 'unpublish' in schedule.trace
