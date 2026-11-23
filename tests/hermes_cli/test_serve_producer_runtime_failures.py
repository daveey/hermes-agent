"""Failure interleavings for the dormant exec walking skeleton."""
import hashlib
import json
import socket
import threading

import pytest

from hermes_cli.serve_producer_protocol import ResourceExhausted, encode_request
from hermes_cli.serve_producer_runtime import Channel, ProjectionRelay
from tests.waiting import wait_until


def relay_for(tmp_path, code):
    provider = tmp_path / 'provider.py'
    provider.write_text(code, encoding='utf-8')
    return ProjectionRelay({'path': str(provider), 'sha256': hashlib.sha256(provider.read_bytes()).hexdigest()},
                           home=tmp_path, runtime=tmp_path / 'runtime', generation='g')


def test_actual_envelope_is_reserved_before_http_acceptance(tmp_path):
    low, high = 1, 1024 * 1024
    while low + 1 < high:
        mid = (low + high) // 2
        try:
            encode_request('budget', {'value': 'x' * mid})
            low = mid
        except ResourceExhausted:
            high = mid
    relay = relay_for(tmp_path, 'def produce(p): return p\n')
    with pytest.raises(ResourceExhausted):
        relay.submit({'value': 'x' * low})
    assert relay.pending_count == 0
    relay.submit({'value': 'small'})
    assert relay.pending_count == 1


def test_late_publication_cannot_resurrect_output_after_drain(tmp_path):
    relay = relay_for(tmp_path, 'def produce(p): return p\n')
    key = relay.submit({'value': 1})
    arrived, release = threading.Event(), threading.Event()

    class Transport:
        def send(self, *args):
            pass

        def receive(self):
            arrived.set()
            assert release.wait(10)
            return {'key': key, 'generation': 'g', 'verb': 'PUBLISHED', 'wire': '{}'}

        def close(self):
            pass

    relay._channel = Transport()
    relay._thread = threading.Thread(target=relay._run)
    relay._thread.start()
    try:
        assert arrived.wait(10)
        assert relay.close(timeout=0.01) is False
        assert relay.read(key) is None
    finally:
        release.set()
        assert relay.close()
    assert relay.read(key) is None and not relay.acknowledged(key)


@pytest.mark.platforms('posix')
def test_uncertain_ping_retires_channel_before_next_authorization_reply(tmp_path):
    relay = relay_for(tmp_path, 'def produce(p): return p\n')
    local, remote = socket.socketpair()
    relay._control = Channel(local)
    try:
        with pytest.raises(TimeoutError):
            relay.ping(timeout=0.01)
        assert relay._control is None
        with pytest.raises(RuntimeError, match='unavailable'):
            relay.ping(capability='wrong')
    finally:
        remote.close()
        relay.close()


@pytest.mark.platforms('posix')
def test_late_ready_after_start_timeout_drains_without_a_missing_control_deadlock(tmp_path):
    from hermes_cli.serve_producer_custody import FamilyCustody

    gate = tmp_path / 'release-init'
    code = ('from pathlib import Path\nimport select\n'
            'def open_provider(config):\n'
            '    gate = Path(config["home"]) / "release-init"\n'
            '    while not gate.exists(): select.select([], [], [], 0.02)\n'
            '    return lambda payload: payload\n')
    relay = relay_for(tmp_path, code)
    try:
        with pytest.raises(TimeoutError):
            relay.start_for_fixture(ready_timeout=0.05)
    finally:
        gate.touch()
        assert relay.close(timeout=15)
    owner = FamilyCustody.acquire(tmp_path / 'runtime', 'panel')
    owner.close()


@pytest.mark.platforms('posix')
def test_quote_heavy_projection_is_bounded_without_crashing_child(tmp_path):
    relay = relay_for(tmp_path, 'import json\ndef produce(payload):\n'
                                '    return json.dumps({"value": "\\\"" * 200000}).encode() if payload["large"] else b"{}"\n')
    try:
        relay.start_for_fixture()
        child = relay.ping()['child_pid']
        key = relay.submit({'large': True})
        wait_until(lambda: relay.pending_count == 0, timeout=15)
        assert relay.read(key) is None
        assert relay.error == 'ResourceExhausted'
        small = relay.submit({'large': False})
        wait_until(lambda: relay.acknowledged(small), timeout=15)
        assert relay.read(small) == b'{}'
        assert relay.ping()['child_pid'] == child
    finally:
        assert relay.close()
