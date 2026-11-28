"""Post-merge safeguards for #4420: revocation fences delivery, rebind fences enqueue,
refused brokers are reaped and a restarted child stays visible to the kernel census."""
import json
import threading
from pathlib import Path

import pytest

from hermes_cli import serve_producer_broker as shared
from hermes_cli.serve_producer_admission import LaunchGate
from hermes_cli.serve_producer_runtime import ProjectionRelay, ResourceExhausted
from tests.hermes_cli.test_serve_producer_partition import Channel, binding, partitioned
from tests.waiting import wait_until


def batch(cursor, *payloads):
    return {'verb': 'EVENTS', 'key': 'events', 'generation': 'g', 'seq': cursor + len(payloads),
            'events': [[cursor + 1 + i, p] for i, p in enumerate(payloads)], 'reset': False}


def plain(tmp_path):
    return ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'r', generation='g')


def test_withdrawn_relay_never_polls_or_delivers_events(tmp_path):
    relay = plain(tmp_path)
    seen = []
    relay.set_event_sink(seen.append)
    relay._event_cursor = 3
    relay._channel = Channel(batch(3, {'n': 4}))
    relay.withdraw()
    relay._poll_events()
    assert seen == [] and relay._channel.sent == []


def test_unbind_during_a_batch_stops_the_rest_of_it(tmp_path):
    relay = partitioned(tmp_path)
    relay.bind_partition(binding('1'))
    seen = []

    def sink(payload):
        seen.append(payload)
        relay.unbind_partition()  # authorization revoked by the first delivered event's consumer

    relay.set_event_sink(sink)
    relay._event_cursor = 3
    relay._channel = Channel(batch(3, {'n': 4}, {'n': 5}))
    relay._poll_events()
    assert seen == [{'n': 4}]


@pytest.mark.parametrize('operation', ['withdraw', 'revoke_generation', 'unbind_partition',
                                        'bind_partition', 'set_event_sink'])
def test_concurrent_revocation_waits_for_inflight_delivery_then_fences_the_rest(tmp_path, operation):
    relay = partitioned(tmp_path)
    relay.bind_partition(binding('1'))
    entered, release, seen = threading.Event(), threading.Event(), []

    def sink(payload):
        seen.append(payload)
        if payload['n'] == 4:
            entered.set()
            assert release.wait(10)

    relay.set_event_sink(sink)
    relay._event_cursor = 3
    relay._channel = Channel(batch(3, {'n': 4}, {'n': 5}))
    poller = threading.Thread(target=relay._poll_events)
    poller.start()
    assert entered.wait(10)
    arguments = {'bind_partition': (binding('2'),), 'set_event_sink': (lambda p: None,)}.get(operation, ())
    revoker = threading.Thread(target=getattr(relay, operation), args=arguments)
    revoker.start()
    try:
        revoker.join(0.3)
        assert revoker.is_alive(), 'withdraw returned while an old-generation delivery was still running'
    finally:
        release.set()
        revoker.join(10)
        poller.join(10)
    assert not revoker.is_alive() and not poller.is_alive()
    assert seen == [{'n': 4}], 'an event was delivered after withdraw() returned'


def test_submit_racing_a_rebind_never_enqueues_old_home_work(tmp_path):
    relay = partitioned(tmp_path)
    relay.bind_partition(binding('1'))
    real = relay._stamped

    def stamp_then_rebind(payload):
        result = real(payload)
        relay.bind_partition(binding('2'))  # the home changes between stamping and enqueueing
        return result

    relay._stamped = stamp_then_rebind
    with pytest.raises(ResourceExhausted, match='home binding changed'):
        relay.submit({'body': {'a': 1}, 'accepted': 0})
    old = binding('1').wire()
    assert all(item[0]['body']['_partition'] != old for item in relay._pending.values())
    assert relay.pending_count == 0
    relay._stamped = real
    assert relay.submit({'body': {'a': 1}, 'accepted': 0}) and relay.pending_count == 1


def _proc_state(pid):
    try:
        text = Path(f'/proc/{pid}/stat').read_text(encoding='ascii', errors='replace')
    except OSError:
        return None
    return text.rsplit(')', 1)[-1].split()[0]


@pytest.mark.platforms('linux')
def test_refused_startup_broker_is_reaped_not_left_a_zombie(tmp_path):
    from tests.hermes_cli.test_serve_producer_shared_broker import complete_cut, write_provider
    runtime = tmp_path / 'runtime'
    relay = ProjectionRelay(write_provider(tmp_path), home=tmp_path, runtime=runtime, generation='unused',
                            initial=None)  # the broker refuses READY without a completed initial
    gate = LaunchGate.acquire(runtime)
    try:
        with pytest.raises(shared.OwnerUnavailable):
            relay.start_for_production(cut=complete_cut(gate, tmp_path), gate=gate, ready_timeout=5)
    finally:
        gate.close()
    pid = relay._spawned.pid
    try:
        assert relay.retire(timeout=5) is True
        assert _proc_state(pid) is None, f'refused broker {pid} left in state {_proc_state(pid)!r}'
    finally:
        relay._spawned.wait(timeout=5)


@pytest.mark.platforms('linux')
def test_restarted_child_is_republished_and_observed_by_the_census(tmp_path):
    from tests.hermes_cli.test_serve_producer_observation import observe
    from tests.hermes_cli.test_serve_producer_shared_broker import produce, start, write_provider
    source = ('from pathlib import Path\nimport os\n'
              'def produce(payload):\n'
              '    marker = payload.get("marker")\n    if marker is None:\n        return {"pid": os.getpid()}\n'
              '    marker = Path(marker)\n'
              '    if not marker.exists():\n        marker.touch()\n        os._exit(7)\n'
              '    return {"pid": os.getpid()}\n')
    relay = start(write_provider(tmp_path, source), tmp_path)
    runtime = tmp_path / 'runtime'
    try:
        old = relay.ping()['child_pid']
        assert observe(runtime)[1].identity.pid == old
        new = produce(relay, marker=str(tmp_path / 'crashed'))['pid']
        assert new != old and relay.ping()['child_pid'] == new
        record = json.loads((runtime / shared.OWNER_FILE).read_text())
        assert record['child_pid'] == new, 'broker.json still names the dead child'
        assert wait_until(lambda: observe(runtime) != (), timeout=10)
        assert observe(runtime)[1].identity.pid == new
    finally:
        assert relay.retire(timeout=10)
    assert not (runtime / shared.OWNER_FILE).exists()
