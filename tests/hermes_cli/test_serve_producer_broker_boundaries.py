"""Deterministic run_shared boundary schedules, complementing the real-exec suite.

Only transport, scheduling and enrollment are seams. Manifest verification,
request authorization and FamilyCustody remain real. Inline workers make stop
publication / receive ordering observable without sleeps or runaway mutants.
"""
from collections import deque
from types import SimpleNamespace
import sys
import threading

import pytest

from hermes_cli import serve_producer_broker as shared
from hermes_cli import serve_producer_custody as custody
from hermes_cli import serve_producer_enrollment as enrollment_module
from hermes_cli import serve_producer_manifest as manifest_module
from hermes_cli import serve_producer_runtime as runtime_module
from tests.hermes_cli.test_serve_producer_shared_broker import write_provider

pytestmark = pytest.mark.platforms('linux')


class EndSchedule(Exception):
    """Finite listener schedule; never used to satisfy a behavioral assertion."""


class Schedule:
    def __init__(self, monkeypatch, tmp_path, *, platform='linux', requests=(),
                 foreign=False, initial=None, first=None, failed=False,
                 stop_on_reap=False, wait_stops=False, stopped_worker=False,
                 socket_exists=True, poll=None, missing_initial=False):
        self.config = {'runtime': str(tmp_path / 'runtime'), 'home': str(tmp_path),
                       'provider': write_provider(tmp_path)}
        if missing_initial:
            pass  # production omits the key; READY must refuse rather than skip produce
        else:
            self.config['initial'] = {'ok': 1} if initial is None else initial
            if first is None:
                first = {'verb': 'PUBLISHED', 'wire': 'initial'}
        self.trace, self.sent, self.errors = [], [], []
        self.peer_receives = 0
        self.waits, self.spawns = [], 0
        self.stop_value = False
        self.socket_path = tmp_path / 'broker.sock'
        if socket_exists:
            self.socket_path.touch()
        self.owner = None
        self.enrollment = None
        self.poll_result = poll
        self.request_queue = deque(requests)
        schedule = self

        class Event:
            def is_set(self):
                return schedule.stop_value

            def set(self):
                schedule.stop_value = True

            def wait(self, delay):
                schedule.waits.append(delay)
                # A seam records whether an already-published stop unnecessarily
                # reaches the backoff; it also injects stop during backoff.
                return wait_stops or schedule.stop_value

        class Child:
            def poll(self):
                return schedule.poll_result

            def wait(self):
                schedule.trace.append('reap')
                if stop_on_reap:
                    schedule.stop_value = True
                return 0

        class Data:
            def __init__(self, bootstrap=True):
                self.reads = 0
                self.bootstrap = bootstrap
                self.sock = SimpleNamespace(shutdown=lambda how: None)

            def receive(self):
                self.reads += 1
                if self.reads == 1:
                    return {'verb': 'HELLO', 'pid': 123}
                if self.bootstrap and 'initial' in schedule.config and self.reads == 2:
                    return first
                if failed:
                    raise EOFError('scheduled child death')
                return {'verb': 'PUBLISHED', 'wire': 'result'}

            def send(self, value, sequence):
                schedule.trace.append(('child-send', value['verb']))

            def close(self):
                schedule.trace.append('data-close')

        class Peer:
            def __init__(self, conn):
                pass

            def receive(self):
                schedule.peer_receives += 1
                if not schedule.request_queue:
                    raise EOFError('finite peer schedule')
                request = dict(schedule.request_queue.popleft())
                request.update(capability=schedule.published['capability'],
                               generation=schedule.published['generation'])
                return request

            def send(self, value, sequence):
                schedule.sent.append(value)

            def close(self):
                pass

        class Thread:
            def __init__(self, *, target, args, **kwargs):
                self.target, self.args = target, args

            def start(self):
                if stopped_worker:
                    schedule.stop_value = True
                self.target(*self.args)

            def join(self):
                pass

        class Listener:
            def __init__(self):
                self.accepted = False

            def settimeout(self, value):
                pass

            def accept(self):
                if self.accepted:
                    raise EndSchedule()
                self.accepted = True
                return SimpleNamespace(), None

            def close(self):
                schedule.trace.append('listener-close')

        # A hashable connection (SimpleNamespace is not hashable).
        class Connection:
            def shutdown(self, how):
                pass

        listener = Listener()
        original_accept = listener.accept

        def accept():
            original_accept()
            return Connection(), None

        listener.accept = accept

        def acquire(runtime, root, *, gate=None):
            schedule.trace.append('enroll')
            schedule.owner = custody.FamilyCustody.acquire(runtime, 'panel')
            schedule.enrollment = SimpleNamespace(
                owner=schedule.owner, ticket=SimpleNamespace(identity=SimpleNamespace(pid=456)))
            return schedule.enrollment

        def spawn(*args, **kwargs):
            schedule.spawns += 1
            schedule.trace.append('spawn')
            return Child(), Data()  # replacements must complete initial publication too

        def publish(runtime, record):
            schedule.published = record

        self.startup = []
        self.channel = SimpleNamespace(
            send=lambda message, sequence: self.startup.append(message),
            close=lambda: self.trace.append('startup-close'))
        monkeypatch.setattr(sys, 'platform', platform)
        monkeypatch.setattr(enrollment_module.BrokerEnrollment, 'acquire', acquire)
        monkeypatch.setattr(runtime_module, 'spawn', spawn)
        monkeypatch.setattr(runtime_module, 'Channel', Peer)
        # Do not change process-global threading: other pytest plugins use it.
        monkeypatch.setattr(shared, 'threading', SimpleNamespace(
            Event=Event, Lock=threading.Lock, Thread=Thread))
        monkeypatch.setattr(shared, '_listener', lambda runtime: listener)
        monkeypatch.setattr(shared, '_peer_uid', lambda conn: shared.os.getuid() + int(foreign))
        monkeypatch.setattr(shared, '_publish_owner', publish)
        monkeypatch.setattr(shared, '_unpublish_owner', lambda runtime: self.trace.append('unpublish'))
        monkeypatch.setattr(shared, '_with_address', lambda runtime, fn: fn(str(self.socket_path)))

    def run(self):
        try:
            gate = SimpleNamespace(_held=lambda: None, close=lambda: self.trace.append('gate-close'))
            shared.run_shared(self.channel, self.config, gate=gate)
        except EndSchedule:
            pass
        except Exception as exc:
            # Mutants must produce assertion failures, not uncaught harness errors.
            self.errors.append(f'{type(exc).__name__}: {exc}')
        finally:
            if self.owner is not None:
                self.owner.close()
        assert self.errors == [], self.errors
        return self


def test_missing_manifest_refused_before_verifier_and_custody(monkeypatch, tmp_path):
    schedule = Schedule(monkeypatch, tmp_path)
    schedule.config['provider']['manifest'] = None
    calls = []
    verify = manifest_module.verify_declared

    def verify_observed(manifest, root):
        calls.append(manifest)
        return verify(manifest, root)

    monkeypatch.setattr(manifest_module, 'verify_declared', verify_observed)
    schedule.run()
    assert schedule.startup == [{'verb': 'REFUSED', 'error': 'manifest'}]
    assert calls == [], 'missing manifest must be refused before calling the verifier'
    assert 'enroll' not in schedule.trace and schedule.spawns == 0


def test_missing_initial_refused_before_custody_and_child(monkeypatch, tmp_path):
    schedule = Schedule(monkeypatch, tmp_path, missing_initial=True).run()
    assert schedule.startup == [{'verb': 'REFUSED', 'error': 'initial'}]
    assert 'enroll' not in schedule.trace and schedule.spawns == 0
    assert 'unpublish' not in schedule.trace


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
@pytest.mark.parametrize('poll', [None, 0])
def test_platform_selects_owner_and_ping_reports_enrollment(monkeypatch, tmp_path, platform, poll):
    schedule = Schedule(monkeypatch, tmp_path, platform=platform, poll=poll,
                        requests=[{'verb': 'PING'}, {'verb': 'STOP'}]).run()
    assert schedule.startup == [{'verb': 'READY'}]
    assert ('enroll' in schedule.trace) is (platform == 'linux')
    pong = schedule.sent[0]
    assert pong['verb'] == 'PONG'
    assert pong['enrollment_pid'] == (456 if platform == 'linux' else None)
    assert pong['child_exited'] is (poll is not None)
    assert schedule.sent[1] == {'verb': 'STOPPING'}


@pytest.mark.parametrize('first', [{'verb': 'REFUSED', 'wire': 'nonempty'},
                                  {'verb': 'PUBLISHED', 'wire': ''}])
def test_initial_publication_requires_both_verb_and_wire(monkeypatch, tmp_path, first):
    schedule = Schedule(monkeypatch, tmp_path, initial={'input': 1}, first=first).run()
    assert schedule.startup == [{'verb': 'REFUSED', 'error': 'child'}]
    assert 'unpublish' not in schedule.trace
    assert schedule.spawns == 1


def test_foreign_uid_never_receives_even_an_authorized_ping(monkeypatch, tmp_path):
    schedule = Schedule(monkeypatch, tmp_path, foreign=True,
                        requests=[{'verb': 'PING'}]).run()
    assert schedule.peer_receives == 0
    assert schedule.sent == []


def test_stop_published_before_worker_start_prevents_receive(monkeypatch, tmp_path):
    schedule = Schedule(monkeypatch, tmp_path, stopped_worker=True,
                        requests=[{'verb': 'PING'}]).run()
    assert schedule.peer_receives == 0
    assert schedule.sent == []


@pytest.mark.parametrize('mode, expected_spawns, expected_waits', [
    ('budget', 3, [1, 2]), ('already-stopped', 1, []), ('stop-during-wait', 1, [1]),
])
def test_restart_guards_refuse_without_extra_spawn_or_reply(
        monkeypatch, tmp_path, mode, expected_spawns, expected_waits):
    schedule = Schedule(monkeypatch, tmp_path, failed=True,
                        stop_on_reap=mode == 'already-stopped',
                        wait_stops=mode == 'stop-during-wait',
                        requests=[{'verb': 'PRODUCE', 'key': 'k', 'payload': {}}]).run()
    assert schedule.spawns == expected_spawns
    assert schedule.waits == expected_waits
    assert schedule.sent == [], 'restart exhaustion must not publish a bogus result'
    assert schedule.stop_value


@pytest.mark.parametrize('exists', [True, False])
def test_socket_cleanup_removes_present_path_and_tolerates_absence(monkeypatch, tmp_path, exists):
    schedule = Schedule(monkeypatch, tmp_path, socket_exists=exists,
                        requests=[{'verb': 'STOP'}]).run()
    assert not schedule.socket_path.exists()
    assert schedule.trace.index('unpublish') < schedule.trace.index('listener-close')
