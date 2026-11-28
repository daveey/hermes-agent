"""Cold, expired or missing mandatory output is 503 Retry-After:1, never a fresh-empty success."""
import time

import pytest

from hermes_cli import serve_producer_runtime as runtime
from hermes_cli.serve_producer_runtime import ProjectionRelay, ResourceExhausted, projection_response


def relay_for(tmp_path):
    return ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'r', generation='g')


def assert_unavailable(response):
    assert response.status_code == 503
    assert response.headers['Retry-After'] == '1'
    assert response.body == b'{"detail":"Producer projection unavailable"}'


def test_cold_output_is_503_and_enqueues_a_demand(tmp_path):
    relay = relay_for(tmp_path)
    assert_unavailable(projection_response(relay, {'_verb': 'status'}))
    assert relay.pending_count == 1  # the request demanded work; it never waited for it


def test_expired_output_is_503_not_stale_success(tmp_path, monkeypatch):
    relay = relay_for(tmp_path)
    body = {'_verb': 'status'}
    key = relay.submit({'body': body, 'accepted': 0})
    relay._outputs[key] = (b'{"stale":true}', time.monotonic() + 5)
    assert projection_response(relay, body).body == b'{"stale":true}'
    real = time.monotonic
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: real() + 6)  # past the 5s prepared-output validity
    assert relay.read(key) is None
    assert_unavailable(projection_response(relay, body))


def test_expiry_boundary_is_exclusive(tmp_path, monkeypatch):
    relay = relay_for(tmp_path)
    key = relay.key_for({'body': {'a': 1}})
    relay._outputs[key] = (b'x', 100.0)
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: 99.999)
    assert relay.read(key) == b'x'
    monkeypatch.setattr(runtime.time, 'monotonic', lambda: 100.0)
    assert relay.read(key) is None


def test_mailbox_exhaustion_is_503(tmp_path):
    relay = relay_for(tmp_path)
    for index in range(runtime.MAX_KEYS):
        relay.submit({'body': {'n': index}, 'accepted': 0})
    assert_unavailable(projection_response(relay, {'n': 'one-too-many'}))


def test_retired_relay_never_answers_even_with_a_held_output(tmp_path):
    relay = relay_for(tmp_path)
    body = {'_verb': 'status'}
    key = relay.key_for({'body': body})
    relay._outputs[key] = (b'{"ok":true}', time.monotonic() + 60)
    assert projection_response(relay, body).status_code == 200
    relay.withdraw()
    assert_unavailable(projection_response(relay, body))


def test_correlated_ack_is_delivered_once_and_a_miss_enqueues_503(tmp_path):
    relay = relay_for(tmp_path)
    body = {'_verb': 'decision-refresh', 'session': 's'}
    assert_unavailable(projection_response(relay, body, consume=True))
    assert relay.pending_count == 1
    key = relay.key_for({'body': body})
    relay._outputs[key] = (b'{"done":1}', time.monotonic() + 5)
    assert projection_response(relay, body, consume=True).body == b'{"done":1}'
    assert relay.read(key) is None  # consumed
    assert_unavailable(projection_response(relay, body, consume=True))


def test_submit_after_withdraw_raises_resource_exhausted(tmp_path):
    relay = relay_for(tmp_path)
    relay.withdraw()
    with pytest.raises(ResourceExhausted):
        relay.submit({'body': {'a': 1}, 'accepted': 0})
