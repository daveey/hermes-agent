"""Dormant exec slice: request callers never execute provider code."""
import hashlib
import json
import time

import pytest

from tests.waiting import wait_until


@pytest.mark.platforms('posix')
def test_exec_projection_and_control_stay_off_the_request_thread(tmp_path):
    from hermes_cli.serve_producer_runtime import ProjectionRelay

    provider = tmp_path / 'provider.py'
    provider.write_text('import os\n'
                        'def produce(payload):\n'
                        '    return {"pid": os.getpid(), "value": payload["value"]}\n', encoding='utf-8')
    spec = {'path': str(provider), 'sha256': hashlib.sha256(provider.read_bytes()).hexdigest()}
    relay = ProjectionRelay(spec, home=tmp_path, runtime=tmp_path / 'runtime', generation='g1')
    try:
        relay.start_for_fixture()
        key = relay.submit({'value': 'one'})
        wait_until(lambda: relay.read(key) is not None, timeout=15)
        result = json.loads(relay.read(key))
        assert result['value'] == 'one'
        assert result['pid'] not in (relay.broker_pid, __import__('os').getpid())
        assert relay.ping(timeout=5)['child_pid'] == result['pid']
        assert relay.acknowledged(key)
    finally:
        relay.close(timeout=10)
    assert not relay.is_alive()


def test_submit_overload_is_atomic_without_starting_any_process(tmp_path):
    from hermes_cli.serve_producer_runtime import ProjectionRelay
    from hermes_cli.serve_producer_protocol import ResourceExhausted

    relay = ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'runtime', generation='g')
    with pytest.raises(ResourceExhausted):
        relay.submit({'value': 'x' * (2 * 1024 * 1024)})
    assert relay.pending_count == 0
    assert relay.broker_pid is None


@pytest.mark.platforms('posix')
def test_wrong_capability_and_generation_are_refused(tmp_path):
    from hermes_cli.serve_producer_runtime import ProjectionRelay

    path = tmp_path / 'auth.py'
    path.write_text('def produce(payload):\n    return payload\n', encoding='utf-8')
    relay = ProjectionRelay({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                            home=tmp_path, runtime=tmp_path / 'runtime', generation='g1')
    try:
        relay.start_for_fixture()
        assert relay.ping(capability='wrong')['verb'] == 'REFUSED'
        assert relay.ping(capability='☃')['verb'] == 'REFUSED'
        assert relay.ping(generation='old')['verb'] == 'REFUSED'
        assert relay.ping()['verb'] == 'PONG'
    finally:
        assert relay.close()


@pytest.mark.platforms('posix')
def test_blocked_child_keeps_off_custody_but_does_not_block_http_or_control(tmp_path):
    from hermes_cli.serve_producer_runtime import ProjectionRelay, projection_response
    from hermes_cli.serve_producer_custody import FamilyCustody, CustodyConflict

    gate = tmp_path / 'unblock'
    started = tmp_path / 'started'
    path = tmp_path / 'blocked.py'
    path.write_text('from pathlib import Path\nimport select\n'
                    'def produce(payload):\n'
                    '    Path(payload["started"]).touch()\n'
                    '    while not Path(payload["gate"]).exists():\n'
                    '        select.select([], [], [], 0.02)\n'
                    '    return payload\n', encoding='utf-8')
    runtime = tmp_path / 'runtime'
    relay = ProjectionRelay({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                            home=tmp_path, runtime=runtime, generation='g1')
    relay.start_for_fixture()
    try:
        relay.submit({'gate': str(gate), 'started': str(started)})
        wait_until(started.exists, timeout=10)
        # Actual blocked producer, not a mocked thread or an idle child.
        assert relay.ping(timeout=2)['verb'] == 'PONG'
        response = projection_response(relay, {'sessions': ['new-chat']})
        assert response.status_code == 503
        assert relay.close(timeout=0.05) is False
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire(runtime, 'panel')
    finally:
        gate.touch()
        assert relay.close(timeout=10)
    successor = FamilyCustody.acquire(runtime, 'panel')
    successor.close()


@pytest.mark.platforms('posix')
def test_actual_crash_restarts_child_before_acknowledging(tmp_path):
    from hermes_cli.serve_producer_runtime import ProjectionRelay

    path = tmp_path / 'crash.py'
    path.write_text('from pathlib import Path\nimport os\n'
                    'def produce(payload):\n'
                    '    marker = Path(payload["marker"])\n'
                    '    if not marker.exists():\n'
                    '        marker.touch()\n'
                    '        os._exit(7)\n'
                    '    return {"pid":os.getpid()}\n', encoding='utf-8')
    relay = ProjectionRelay({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                            home=tmp_path, runtime=tmp_path / 'runtime', generation='g1')
    try:
        relay.start_for_fixture()
        old = relay.ping()['child_pid']
        key = relay.submit({'marker': str(tmp_path / 'crashed')})
        wait_until(lambda: relay.acknowledged(key), timeout=15)
        assert json.loads(relay.read(key))['pid'] != old
        assert relay.ping()['child_pid'] != old
    finally:
        assert relay.close()


def test_mailbox_has_hard_count_bound_and_no_partial_overload(tmp_path):
    from hermes_cli.serve_producer_runtime import ProjectionRelay
    from hermes_cli.serve_producer_protocol import ResourceExhausted

    relay = ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'runtime', generation='g')
    for value in range(256):
        relay.submit({'value': value})
    with pytest.raises(ResourceExhausted):
        relay.submit({'value': 'overflow'})
    assert relay.pending_count == 256
    relay.submit({'value': 0})
    assert relay.pending_count == 256


def test_production_start_is_unavailable_without_real_admission(tmp_path, monkeypatch):
    from hermes_cli.serve_producer_runtime import ProjectionRelay

    monkeypatch.delenv('PYTEST_CURRENT_TEST')
    relay = ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'runtime', generation='g')
    with pytest.raises(RuntimeError, match='admission not integrated'):
        relay.start_for_fixture()
    assert relay.broker_pid is None


@pytest.mark.platforms('posix')
def test_off_in_process_custody_blocks_isolated_start_until_drain(tmp_path):
    from hermes_cli.serve_producer_custody import CustodyConflict, FamilyCustody
    from hermes_cli.serve_producer_runtime import ProjectionRelay

    runtime = tmp_path / 'runtime'
    path = tmp_path / 'off.py'
    path.write_text('def produce(payload):\n    return payload\n', encoding='utf-8')
    holder = FamilyCustody.acquire(runtime, 'panel')
    relay = ProjectionRelay({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                            home=tmp_path, runtime=runtime, generation='g1')
    try:
        with pytest.raises((TimeoutError, EOFError, OSError, ValueError)):
            relay.start_for_fixture(ready_timeout=1)
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire(runtime, 'panel')
    finally:
        relay.close(timeout=10)
        holder.close()
    successor = FamilyCustody.acquire(runtime, 'panel')
    successor.close()
    isolated = ProjectionRelay({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                               home=tmp_path, runtime=runtime, generation='g1')
    try:
        isolated.start_for_fixture()
        assert isolated.ping()['verb'] == 'PONG'
        with pytest.raises(CustodyConflict):
            FamilyCustody.acquire(runtime, 'panel')
    finally:
        assert isolated.close(timeout=10)
    released = FamilyCustody.acquire(runtime, 'panel')
    released.close()


@pytest.mark.platforms('posix')
def test_http_acceptance_deadlines_are_not_extended_by_child_delay(tmp_path):
    from hermes_cli.serve_producer_runtime import ProjectionRelay

    started = tmp_path / 'started'
    gate = tmp_path / 'gate'
    path = tmp_path / 'deadlines.py'
    path.write_text(
        'import time\nfrom pathlib import Path\nimport select\n'
        'def produce(payload):\n'
        '    Path(payload["started"]).touch()\n'
        '    while not Path(payload["gate"]).exists():\n'
        '        select.select([], [], [], 0.02)\n'
        '    now = time.monotonic()\n'
        '    accepted = payload["accepted"]\n'
        '    if now < accepted or accepted + 900 <= now:\n'
        '        raise RuntimeError("expired browser interest")\n'
        '    return {"interest": accepted + 900 - now, "visibility": accepted + 75 - now}\n',
        encoding='utf-8')
    relay = ProjectionRelay({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
                            home=tmp_path, runtime=tmp_path / 'runtime', generation='g1')
    try:
        relay.start_for_fixture()
        accepted = time.monotonic()
        key = relay.submit({'accepted': accepted, 'started': str(started), 'gate': str(gate)})
        wait_until(started.exists, timeout=10)
        delay_until = time.monotonic() + 1
        wait_until(lambda: time.monotonic() >= delay_until, timeout=5)
        gate.touch()
        wait_until(lambda: relay.acknowledged(key), timeout=15)
        result = json.loads(relay.read(key))
        assert 0 < result['interest'] < 899.5
        assert 0 < result['visibility'] < 74.5
        expired = relay.submit({'accepted': accepted - 901, 'started': str(tmp_path / 'x'), 'gate': str(gate)})
        wait_until(lambda: relay.pending_count == 0, timeout=15)
        assert relay.read(expired) is None
        assert relay.error == 'RuntimeError'
    finally:
        assert relay.close(timeout=10)
