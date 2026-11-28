"""Behavioral seams for shared-owner runtime guards (deterministic fakes, no live broker)."""
import socket

import pytest

from hermes_cli import serve_producer_broker as shared
from hermes_cli import serve_producer_runtime as runtime_mod
from hermes_cli.serve_producer_protocol import ProtocolError
from hermes_cli.serve_producer_runtime import ProjectionRelay

SLEEP_BOUND = 40


def must_succeed(action, message):
    try:
        return action()
    except Exception as exc:
        pytest.fail(f'{message}: {type(exc).__name__}: {exc}')


def make_relay(tmp_path, **kwargs):
    return ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'runtime', generation='g', **kwargs)


class FakeSock:
    def __init__(self):
        self.timeout = None
        self.shutdowns = []
        self.closed = 0

    def settimeout(self, timeout):
        self.timeout = timeout

    def shutdown(self, how):
        self.shutdowns.append(how)

    def close(self):
        self.closed += 1


class FakeHelloChannel:
    def __init__(self, hello):
        self.hello = hello
        self.sock = FakeSock()
        self.closed = False

    def receive(self):
        return self.hello

    def close(self):
        self.closed = True


class FakeChannel:
    def __init__(self):
        self.sock = FakeSock()
        self.closed = 0

    def close(self):
        self.closed += 1


class FakeControl:
    def __init__(self):
        self.sock = FakeSock()
        self.sent = []
        self.closed = 0

    def send(self, value, request_id='message'):
        self.sent.append(value)

    def receive(self):
        return {'verb': 'OK'}

    def close(self):
        self.closed += 1


class FakeThread:
    def __init__(self, *, stays_alive=False):
        self.stays_alive = stays_alive
        self.joins = []

    def join(self, timeout=None):
        self.joins.append(timeout)

    def is_alive(self):
        return self.stays_alive


class Clock:
    def __init__(self, start=1_000.0):
        self.now = start
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if len(self.sleeps) >= SLEEP_BOUND:
            raise AssertionError('wait loop ignored owner death and deadline')
        self.sleeps.append(seconds)
        self.now += seconds


class SpawnTripwire:
    def close(self):
        pytest.fail('already-started relay must not take the spawn path')


def install_clock(monkeypatch, clock):
    monkeypatch.setattr(runtime_mod.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(runtime_mod.time, 'sleep', clock.sleep)
    return clock


def install_alive(monkeypatch, alive):
    monkeypatch.setattr(shared, 'owner_alive', alive)
    return alive


# ---- config initial -----------------------------------------------------------------

def test_absent_initial_is_not_copied_into_config(tmp_path):
    relay = make_relay(tmp_path)
    assert 'initial' not in relay.config
    explicit = make_relay(tmp_path, initial=None)
    assert 'initial' not in explicit.config


def test_provided_initial_is_copied_into_config(tmp_path):
    payload = {'snapshot': 1}
    relay = make_relay(tmp_path, initial=payload)
    assert relay.config['initial'] == payload


# ---- _start_shared already-started ---------------------------------------------------

def test_start_shared_refuses_existing_process_even_without_owner(tmp_path):
    relay = make_relay(tmp_path)
    relay._process = object()
    relay._owner = None
    with pytest.raises(RuntimeError, match='already started'):
        relay._start_shared(SpawnTripwire(), 1)


def test_start_shared_refuses_existing_owner_even_without_process(tmp_path):
    relay = make_relay(tmp_path)
    relay._process = None
    relay._owner = {'pid': 7}
    with pytest.raises(RuntimeError, match='already started'):
        relay._start_shared(SpawnTripwire(), 1)


def test_start_shared_first_attach_is_not_already_started(tmp_path, monkeypatch):
    relay = make_relay(tmp_path)
    digest = shared.compatibility(relay.config['provider'], relay.config['home'])
    info = {'digest': digest, 'pid': 9, 'generation': 'g', 'capability': 'c' * 64}
    monkeypatch.setattr(shared, 'read_owner', lambda runtime: info)
    attached = []
    monkeypatch.setattr(relay, '_attach_owner', lambda record, timeout: attached.append((record, timeout)))
    must_succeed(lambda: relay._start_shared(SpawnTripwire(), 2),
                 'first start must not be treated as already started')
    assert attached == [(info, 2)]


# ---- _spawn_owner READY hello --------------------------------------------------------

def test_spawn_owner_accepts_ready_hello(tmp_path, monkeypatch):
    relay = make_relay(tmp_path)
    channel = FakeHelloChannel({'verb': 'READY'})
    process = object()

    def fake_spawn(role, config, *, custody_fd=None, detach=False, gate_fd=None):
        assert role == 'broker' and config['shared'] is True and detach is True
        return process, channel

    monkeypatch.setattr(runtime_mod, 'spawn', fake_spawn)
    must_succeed(lambda: relay._spawn_owner(1.5), 'READY hello must be accepted')
    assert relay._spawned is process
    assert channel.closed is True
    assert channel.sock.timeout == 1.5


def test_spawn_owner_refuses_non_ready_hello(tmp_path, monkeypatch):
    relay = make_relay(tmp_path)
    channel = FakeHelloChannel({'verb': 'ERROR', 'error': 'nope'})
    monkeypatch.setattr(runtime_mod, 'spawn', lambda *a, **k: (object(), channel))
    with pytest.raises(ProtocolError, match='refused'):
        relay._spawn_owner(1)
    assert channel.closed is True


# ---- _detach_owner attachment presence ----------------------------------------------

def test_detach_owner_with_no_attachments_succeeds(tmp_path):
    relay = make_relay(tmp_path)
    relay._control = None
    relay._channel = None
    relay._thread = None
    assert must_succeed(lambda: relay._detach_owner(0.1),
                        'detach with missing attachments must succeed') is True


def test_detach_owner_closes_present_control_channel_and_joins_thread(tmp_path):
    relay = make_relay(tmp_path)
    control, channel, thread = FakeControl(), FakeChannel(), FakeThread(stays_alive=True)
    relay._control, relay._channel, relay._thread = control, channel, thread
    result = must_succeed(lambda: relay._detach_owner(0.25),
                          'detach with present attachments must succeed')
    assert control.closed == 1 and relay._control is None
    assert channel.sock.shutdowns == [socket.SHUT_RDWR]
    assert channel.closed == 1
    assert thread.joins == [0.25]
    assert result is False


def test_detach_owner_reports_thread_exit_when_join_finishes(tmp_path):
    relay = make_relay(tmp_path)
    thread = FakeThread(stays_alive=False)
    relay._control = None
    relay._channel = FakeChannel()
    relay._thread = thread
    result = must_succeed(lambda: relay._detach_owner(1),
                          'detach after a finished thread must succeed')
    assert thread.joins == [1]
    assert result is True


# ---- retire owner / control / wait loop ---------------------------------------------

def test_retire_without_owner_uses_close(tmp_path):
    relay = make_relay(tmp_path)
    relay._owner = None
    seen = []

    def close(*, timeout=10):
        seen.append(timeout)
        return 'closed'

    relay.close = close
    result = must_succeed(lambda: relay.retire(timeout=3),
                          'retire with no owner must use close')
    assert result == 'closed' and seen == [3]


def test_retire_with_owner_does_not_take_the_absent_close_path(tmp_path, monkeypatch):
    relay = make_relay(tmp_path)
    owner = {'pid': 11, 'start': '1'}
    relay._owner = owner
    relay._control = None
    relay.close = lambda **k: pytest.fail('owned retire must not take the owner-absent close path')
    install_alive(monkeypatch, lambda info: False)
    clock = install_clock(monkeypatch, Clock())
    result = must_succeed(lambda: relay.retire(timeout=0.2),
                          'retire with a dead owner and no control must succeed')
    assert clock.sleeps == []
    assert result is True


def test_retire_sends_stop_when_control_is_present(tmp_path, monkeypatch):
    relay = make_relay(tmp_path)
    owner = {'pid': 12, 'start': '1'}
    control = FakeControl()
    relay._owner, relay._control = owner, control
    install_alive(monkeypatch, lambda info: False)
    install_clock(monkeypatch, Clock())
    result = must_succeed(lambda: relay.retire(timeout=0.2),
                          'retire with control must send STOP then detach')
    assert any(item.get('verb') == 'STOP' for item in control.sent)
    assert result is True


def test_retire_waits_until_owner_exits_within_deadline(tmp_path, monkeypatch):
    relay = make_relay(tmp_path)
    owner = {'pid': 13, 'start': '1'}
    relay._owner, relay._control = owner, None
    calls = {'n': 0}

    def alive(info):
        assert info is owner
        calls['n'] += 1
        return calls['n'] <= 2

    install_alive(monkeypatch, alive)
    clock = install_clock(monkeypatch, Clock())
    result = must_succeed(lambda: relay.retire(timeout=1),
                          'retire must wait for a still-live owner')
    assert clock.sleeps
    assert result is True


def test_retire_returns_false_when_owner_outlives_deadline(tmp_path, monkeypatch):
    relay = make_relay(tmp_path)
    owner = {'pid': 14, 'start': '1'}
    relay._owner, relay._control = owner, None
    install_alive(monkeypatch, lambda info: True)
    clock = install_clock(monkeypatch, Clock())
    result = must_succeed(lambda: relay.retire(timeout=0.1),
                          'retire must observe the deadline while the owner lives')
    assert clock.sleeps
    assert result is False


def test_retire_false_when_owner_exits_but_relay_thread_stays(tmp_path, monkeypatch):
    relay = make_relay(tmp_path)
    owner = {'pid': 15, 'start': '1'}
    thread = FakeThread(stays_alive=True)
    relay._owner, relay._control, relay._thread = owner, None, thread
    install_alive(monkeypatch, lambda info: False)
    install_clock(monkeypatch, Clock())
    result = must_succeed(lambda: relay.retire(timeout=0.2),
                          'retire must require both owner exit and detach')
    assert thread.joins
    assert result is False
