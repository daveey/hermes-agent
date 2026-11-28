"""Exec producer relay: fixture start stays pytest-only; production start is gated.

HTTP threads never wait for ACK: they enqueue and return prepared bytes or 503.
The fixture mailbox is not the production architecture — production start uses
the same private IPC plus the child's WAL projection/event ring.
"""
from __future__ import annotations

import hashlib
import copy
import array
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

from hermes_cli.serve_producer_protocol import (
    FrameDecoder, ProtocolError, RequestAssembly, ResourceExhausted, encode_request,
)

MAX_KEYS = 256
MAX_BYTES = 1024 * 1024


def projection_response(relay, body, *, consume=False):
    """Shared prepared-wire adapter; no source capture or IPC on HTTP threads.

    ``consume`` is for correlated acknowledgements (decision refresh): an enqueue is never an ACK,
    only a later retry may take the one owner publication; a miss enqueues and answers 503.
    """
    from starlette.responses import Response

    try:
        accepted = {'body': body, 'accepted': time.monotonic()}
        if consume:
            wire = relay.read(relay.key_for(accepted), consume=True)
            if wire is None:
                relay.submit(accepted)
        else:
            key = relay.submit(accepted)
            wire = relay.read(key)
    except ResourceExhausted:
        wire = None
    if wire is None:
        return Response(b'{"detail":"Producer projection unavailable"}', status_code=503,
                        media_type='application/json', headers={'Retry-After': '1'})
    return Response(wire, media_type='application/json')


class Channel:
    """Private inherited Unix stream: bounded messages, never pickle."""

    def __init__(self, sock):
        self.sock = sock
        self.decoder = FrameDecoder()
        self.assembly = RequestAssembly()
        self.ready = []

    def send(self, value, request_id='message'):
        for frame in encode_request(request_id, value):
            self.sock.sendall(frame)

    def receive(self):
        while not self.ready:
            data = self.sock.recv(65536)  # also bounds allocations per decoder call
            if not data:
                raise EOFError('producer channel closed')
            for frame in self.decoder.feed(data):
                item = self.assembly.accept(frame)
                if item is not None:
                    self.ready.append(item[1])
        return self.ready.pop(0)

    def close(self):
        self.sock.close()


def spawn(role, config, *, custody_fd=None, detach=False, gate_fd=None):
    """Fixture exec path; no generic Hermes/plugin/model bootstrap in a child."""
    from hermes_cli._startup_fast import project_root_str
    from tools.environments.local import build_subprocess_env

    parent, child = socket.socketpair()
    script = Path(__file__).with_name('serve_producer_process.py')
    inherited = (child.fileno(),) + (() if custody_fd is None else (custody_fd,))
    if gate_fd is not None:
        inherited += (gate_fd,)
    command = [sys.executable, '-I', str(script), role, str(child.fileno()), project_root_str()]
    if custody_fd is not None or gate_fd is not None:
        command.append(str(custody_fd) if custody_fd is not None else '-1')
    if gate_fd is not None:
        command.append(str(gate_fd))
    try:
        process = subprocess.Popen(command, pass_fds=inherited,
            env=build_subprocess_env(extra={'HERMES_HOME': config['home']}),
            stdin=subprocess.DEVNULL,
            # detached-spawn: ok — shared owner outlives its spawning serve; holds family custody, exits via STOP/EOF only
            start_new_session=detach)
    except BaseException:
        parent.close()
        raise
    finally:
        child.close()
    channel = Channel(parent)
    channel.send(config, 'init')  # capability only travels over the inherited private FD
    return process, channel


class ProjectionRelay:
    """HTTP calls only bounded memory operations; all IPC uses the relay thread.

    The fixture has one in-flight producer operation, plus an atomic 256-key /
    1MiB mailbox. An ACK is visible only after a child publication was received.
    Generation changes revoke all cached bytes and pending work locally.
    """

    def __init__(self, provider, *, home, runtime, generation, initial=None):
        self.config = {'provider': provider, 'home': str(home), 'runtime': str(runtime),
                       'generation': generation, 'capability': secrets.token_hex(32)}
        if initial is not None:
            self.config['initial'] = initial  # verified by the shared owner BEFORE it publishes READY
        # A partitioned provider serves several canonical homes inside ONE child: every request and event poll
        # then carries the server-side home binding, and nothing is accepted without one.
        self.partitioned = bool(isinstance(provider, dict) and provider.get('partitioned'))
        self._partition = None  # the bound HomeBinding wire dict; set only by bind_partition
        self._partition_identity = None
        self.partition_check = None  # optional callable(identity-tuple) -> bool, consulted before event delivery
        self._owner = None  # the shared owner's published record once attached
        self._spawned = None
        self._lock = threading.Lock()
        self._delivery = threading.RLock()  # callbacks may revoke/rebind reentrantly
        self._sink_revision = 0
        self._pending = OrderedDict()
        self._outputs = OrderedDict()
        self._bytes = 0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._process = None
        self._channel = None
        self._control = None
        self._control_lock = threading.Lock()
        self._sequence = 0
        self.error = None
        self._event_sink = None
        self._event_cursor = None  # None: adopt the child's head; -1: force reset+snapshot
        self.event_polls = 0
        self.event_resets = 0
        self._revision = 0
        self._withdrawn = False  # set once by withdraw(): this generation can never serve or attach again

    @property
    def retired(self):
        return self._stop.is_set() or self._withdrawn

    @property
    def broker_pid(self):
        if self._owner is not None:
            return self._owner['pid']
        return self._process.pid if self._process else None

    @property
    def pending_count(self):
        with self._lock:
            return len(self._pending)

    def start_for_fixture(self, *, ready_timeout=10):
        # Fixture mailbox/start remains pytest-only. Production uses start_for_production.
        if 'PYTEST_CURRENT_TEST' not in os.environ:
            raise RuntimeError('strict production admission not integrated')
        self._boot(ready_timeout=ready_timeout)

    def start_for_production(self, *, cut, gate, ready_timeout=10):
        """Launch under a held COMPLETE LaunchGate. Never selected by real serve here."""
        from hermes_cli.serve_producer_admission import AdmissionUnavailable

        if cut is None or gate is None:
            raise AdmissionUnavailable('production start requires gate-held COMPLETE cut')
        if not getattr(cut, 'complete', False):
            raise AdmissionUnavailable('incomplete producer-admission evidence')
        gate.require(cut, boot_id=cut.boot_id, generation=cut.generation,
                     installation=cut.installation)
        self._start_shared(gate, ready_timeout)

    def _start_shared(self, gate, ready_timeout):
        """Attach to the installation's ONE shared owner, spawning it only when none is published.

        Never a relay per serve: a published READY owner is joined (same epoch), an incompatible
        one makes this serve an unavailable observer, and a broker that failed to take custody
        (another serve won the race, or a surviving family still fences) is never replaced.
        """
        from hermes_cli import serve_producer_broker as shared

        if self._process is not None or self._owner is not None:
            raise RuntimeError('relay already started')
        runtime = Path(self.config['runtime'])
        digest = shared.compatibility(self.config['provider'], self.config['home'])
        info = shared.read_owner(runtime)
        if info is None:
            gate.require_runtime(runtime)  # inherited description bridges census to broker custody/READY
            spawn_error = None
            try:
                self._spawn_owner(ready_timeout, gate=gate)
            except (OSError, EOFError, ValueError, ProtocolError) as exc:
                spawn_error = exc
            deadline = time.monotonic() + (ready_timeout if spawn_error else 0)
            while True:
                info = shared.read_owner(runtime)
                if info is not None or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)  # a racing serve's broker may still be publishing
            if info is None:
                raise shared.OwnerUnavailable(f'no ready shared owner: {spawn_error!r}')
        if info['digest'] != digest:
            raise shared.IncompatibleOwner('shared owner runs a different provider/home')
        self._attach_owner(info, ready_timeout)

    def _spawn_owner(self, ready_timeout, *, gate=None):
        config = dict(self.config, shared=True)
        process, channel = spawn('broker', config, detach=True,
                                 gate_fd=gate.fd if gate is not None else None)
        self._spawned = process
        channel.sock.settimeout(ready_timeout)
        ready = False
        try:
            hello = channel.receive()
            ready = hello.get('verb') == 'READY'
        finally:
            channel.close()
            if not ready:
                try:
                    process.wait(timeout=ready_timeout)
                except subprocess.TimeoutExpired:
                    pass  # retain a live owner; close/retire must still report a failed drain
        if hello.get('verb') != 'READY':
            raise ProtocolError('shared owner refused: ' + str(hello.get('error')))

    def _attach_owner(self, info, timeout):
        from hermes_cli import serve_producer_broker as shared

        runtime = Path(self.config['runtime'])
        data = control = None
        try:
            data = shared.connect_owner(runtime, timeout)
            control = shared.connect_owner(runtime, timeout)
            self.config.update(capability=info['capability'], generation=info['generation'])
            self._control = Channel(control)
            self._control.sock.settimeout(timeout)
            self._control.send(self._envelope('PING'), secrets.token_hex(8))
            if self._control.receive().get('verb') != 'PONG':
                raise ProtocolError('shared owner refused authorization')
            self._control.sock.settimeout(None)
            data.settimeout(None)
        except BaseException:
            for sock in (data, control):
                if sock is not None:
                    sock.close()
            self._control = None
            raise
        self._channel = Channel(data)
        self._owner = info
        self._thread = threading.Thread(target=self._run, name='producer-projection-relay', daemon=True)
        self._thread.start()

    def _boot(self, *, ready_timeout=10):
        if self._process is not None:
            raise RuntimeError('relay already started')
        self._process, self._channel = spawn('broker', self.config)
        self._channel.sock.settimeout(ready_timeout)
        try:
            hello = self._channel.receive()
        except (OSError, EOFError, ValueError):
            self._channel.close()
            raise
        if hello.get('verb') != 'READY':
            raise ProtocolError('broker did not start')
        self._channel.sock.settimeout(None)
        # A second private socket is inherited via SCM_RIGHTS, not a public listener.
        control, broker_control = socket.socketpair()
        self._channel.sock.sendmsg([b'C'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                           array.array('i', [broker_control.fileno()]))])
        broker_control.close()
        self._control = Channel(control)
        self._thread = threading.Thread(target=self._run, name='producer-projection-relay', daemon=True)
        self._thread.start()

    def set_event_sink(self, sink):
        """Register the serve-side consumer of ordered child events (one sink per relay)."""
        with self._lock:
            if sink is not None and (self._stop.is_set() or self._withdrawn):
                raise RuntimeError('relay retired: a retained callback cannot attach it again')
            self._event_sink = sink
            self._event_cursor = None
            self._sink_revision += 1
        with self._delivery:
            pass  # return only after any prior sink call completed
        self._wake.set()

    def _poll_events(self):
        """Pull one bounded batch; deliver in order, or one reset+snapshot on any gap/overflow."""
        with self._lock:
            if self._stop.is_set() or self._withdrawn:
                return
            sink, cursor, revision = self._event_sink, self._event_cursor, self._revision
            sink_revision = self._sink_revision
            partition, identity = self._partition, self._partition_identity
        if sink is None:
            return
        extra = {}
        if self.partitioned:
            check = self.partition_check
            if partition is None:
                return  # no authorized home: no event is requested, none delivered
            if check is not None and not check(identity):
                self.unbind_partition()  # roster/lifecycle/authorization changed: revoke before delivery
                return
            extra['partition'] = partition
        self._sequence += 1
        self._channel.send(self._envelope('EVENTS', after=-1 if cursor is None else cursor, **extra),
                           str(self._sequence))
        result = self._channel.receive()
        with self._lock:
            if self._stop.is_set() or self._withdrawn or revision != self._revision or sink_revision != self._sink_revision:
                return  # revoked while in flight: nothing from the old generation is delivered
            if result.get('generation') != self.config['generation'] or result.get('key') != 'events':
                raise ProtocolError('event generation mismatch')
            self.event_polls += 1
            if result.get('verb') != 'EVENTS':
                self._event_cursor = -1
                return
            head = result.get('seq')
            events = result.get('events') or []
            if type(head) is not int or head < 0 or len(events) > 64:
                raise ProtocolError('event batch shape')
            deliver = []
            if result.get('reset') or cursor in (None, -1):
                self.event_resets += 1
                deliver = [dict(result.get('snapshot') or {}, reset=True)]
                self._event_cursor = head
            else:
                expect = cursor
                for item in events:
                    seq, payload = item
                    if seq != expect + 1:
                        self._event_cursor = -1  # gap: next poll asks for reset+snapshot
                        deliver = []
                        break
                    deliver.append(payload)
                    expect = seq
                else:
                    self._event_cursor = expect
        for payload in deliver:
            with self._delivery:
                with self._lock:
                    if (self._stop.is_set() or self._withdrawn or revision != self._revision
                            or sink_revision != self._sink_revision):
                        return
                try:
                    sink(payload)
                except Exception:  # health: allow BLE001 -- a consumer failure must not stop projection custody; surfaced via self.error
                    self.error = 'event sink failed'

    def revoke_generation(self):
        """Atomically drop queued work, prepared outputs and event cursor for this generation."""
        with self._lock:
            self._pending.clear()
            self._outputs.clear()
            self._bytes = 0
            self._event_cursor = None
            self._revision += 1  # output generation; the child's IPC generation is its auth and stays fixed
        with self._delivery:
            pass  # revision is revoked before waiting; no later batch item can acquire authorization

    def withdraw(self):
        """Retire this generation for good (lifecycle drain): revoke everything, then refuse all further use."""
        with self._lock:
            self._withdrawn = True
        self.revoke_generation()

    @property
    def partition_identity(self):
        with self._lock:
            return self._partition_identity

    def bind_partition(self, binding):
        """Bind this relay to one server-derived home. A changed identity atomically revokes queued work,
        prepared outputs, ACKs and the event cursor before any later reply; an identical one is a no-op."""
        wire = binding.wire()
        with self._lock:
            changed = self._partition_identity != binding.identity
            if changed:
                self._partition = wire
                self._partition_identity = binding.identity
                self._pending.clear()
                self._outputs.clear()
                self._bytes = 0
                self._event_cursor = None
                self._revision += 1
        with self._delivery:
            pass  # an identical bind also waits for an earlier concurrent rebind's sink
        self._wake.set()
        return changed

    def unbind_partition(self):
        """The bound home is no longer authorized: revoke everything and refuse all requests until rebound."""
        with self._lock:
            changed = self._partition_identity is not None
            if changed:
                self._partition = None
                self._partition_identity = None
                self._pending.clear()
                self._outputs.clear()
                self._bytes = 0
                self._event_cursor = None
                self._revision += 1
        with self._delivery:
            pass  # even an idempotent unbind fences a concurrent revoker's in-flight sink
        return changed

    def _stamped(self, payload):
        """The payload with the relay's own partition claim; a caller-supplied claim is always overwritten."""
        if not self.partitioned:
            return payload
        with self._lock:
            partition = None if self._partition is None else dict(self._partition)
        if partition is None:
            raise ResourceExhausted('no authorized home')
        stamped = dict(payload)
        body = dict(stamped['body'] if 'body' in stamped else stamped)
        body['_partition'] = partition
        if 'body' in stamped:
            stamped['body'] = body
            return stamped
        return body

    def submit(self, payload):
        with self._lock:
            revision = self._revision
        payload = self._stamped(payload)
        identity = payload.get('body', payload)
        key = hashlib.sha256(b''.join(encode_request('budget', identity))).hexdigest()
        encoded = b''.join(encode_request('x' * 32, self._envelope('PRODUCE', key=key, payload=payload)))
        payload = copy.deepcopy(payload)  # bounded/JSON-validated before retention
        size = len(encoded)
        with self._lock:
            if self._stop.is_set() or self._withdrawn:
                raise ResourceExhausted('relay draining')
            if revision != self._revision:
                raise ResourceExhausted('home binding changed')
            if key not in self._pending:
                if len(self._pending) >= MAX_KEYS or self._bytes + size > MAX_BYTES:
                    raise ResourceExhausted('relay mailbox full')
                self._pending[key] = (payload, size)
                self._bytes += size
        self._wake.set()
        return key

    def key_for(self, payload):
        payload = self._stamped(payload)
        identity = payload.get('body', payload)
        return hashlib.sha256(b''.join(encode_request('budget', identity))).hexdigest()

    def read(self, key, *, consume=False):
        with self._lock:
            if self._stop.is_set() or self._withdrawn:
                return None
            value = self._outputs.get(key)
            if value and value[1] > time.monotonic():
                if consume:
                    del self._outputs[key]  # a correlated ACK is delivered once
                return value[0]
            return None

    def acknowledged(self, key):
        with self._lock:
            return not (self._stop.is_set() or self._withdrawn) and key in self._outputs and key not in self._pending

    def _envelope(self, verb, **extra):
        return dict(capability=self.config['capability'], generation=self.config['generation'], verb=verb, **extra)

    def _run(self):
        try:
            while not self._stop.is_set():
                self._wake.wait(0.25 if self._event_sink is not None else None)
                self._wake.clear()
                while not self._stop.is_set():
                    with self._lock:
                        item = next(iter(self._pending.items()), None)
                        revision = self._revision
                    if item is None:
                        self._poll_events()
                        break
                    key, (payload, size) = item
                    self._sequence += 1
                    self._channel.send(self._envelope('PRODUCE', key=key, payload=payload), str(self._sequence))
                    result = self._channel.receive()
                    if result.get('key') != key or result.get('generation') != self.config['generation']:
                        raise ProtocolError('projection generation mismatch')
                    with self._lock:
                        if self._stop.is_set():
                            return
                        if revision != self._revision:
                            continue  # revoked in flight: neither pending, bytes nor output survive
                        self._pending.pop(key)
                        self._bytes -= size
                        if result.get('verb') == 'PUBLISHED':
                            wire = result['wire'].encode('utf-8')
                            self._outputs[key] = (wire, time.monotonic() + 5)
                            while len(self._outputs) > MAX_KEYS or sum(len(v[0]) for v in self._outputs.values()) > MAX_BYTES:
                                self._outputs.popitem(last=False)
                        else:
                            # A failed refresh cannot acknowledge an older publication.
                            self._outputs.pop(key, None)
                            self.error = result.get('error', 'producer unavailable')
        except (OSError, EOFError, ValueError) as exc:
            self.error = type(exc).__name__
            self._stop.set()
            with self._lock:
                self._pending.clear()
                self._outputs.clear()
                self._bytes = 0

    def ping(self, *, timeout=5, capability=None, generation=None):
        with self._control_lock:
            if self._control is None:
                raise RuntimeError('control channel unavailable')
            self._control.sock.settimeout(timeout)
            request = self._envelope('PING')
            if capability is not None:
                request['capability'] = capability
            if generation is not None:
                request['generation'] = generation
            try:
                self._control.send(request, secrets.token_hex(8))
                return self._control.receive()
            except (OSError, EOFError, ValueError):
                self._control.close()
                self._control = None
                raise

    def _detach_owner(self, timeout):
        """Leave the shared owner running for its other clients; only this serve's attachment ends."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._outputs.clear()
            self._pending.clear()
            self._bytes = 0
        with self._control_lock:
            if self._control is not None:
                self._control.close()
                self._control = None
        if self._channel is not None:
            try:
                self._channel.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # owner already gone
            self._channel.close()
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def retire(self, *, timeout: float = 10):
        """Ask the shared owner to drain, then report whether its process actually exited.

        False means a child or descendant still holds the kernel fences: retained, never killed,
        taken over or unlocked. The owner withdraws READY before it drains anything.
        """
        from hermes_cli import serve_producer_broker as shared

        owner = self._owner
        if owner is None:
            return self.close(timeout=timeout)
        with self._control_lock:
            if self._control is not None:
                try:
                    self._control.sock.settimeout(timeout)
                    self._control.send(self._envelope('STOP'), secrets.token_hex(8))
                    self._control.receive()
                except (OSError, EOFError, ValueError):
                    pass  # an already-draining owner may close first; exit below is the proof
        deadline = time.monotonic() + timeout
        while shared.owner_alive(owner) and time.monotonic() < deadline:
            time.sleep(0.02)
        exited = not shared.owner_alive(owner)
        # The spawning serve remains the broker's waitpid owner even in shared mode.
        # Its unreaped zombie has no observable argv and would make the next strict cut INCOMPLETE.
        if self._spawned is not None:
            try:
                self._spawned.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                exited = False  # surviving broker: no kill and no successor admission
        detached = self._detach_owner(max(0.0, deadline - time.monotonic()) + 0.1)
        return exited and detached

    def close(self, *, timeout: float = 10):
        if self._owner is not None:
            return self._detach_owner(timeout)
        self._stop.set()
        self._wake.set()
        with self._lock:
            self._outputs.clear()
        with self._control_lock:
            if self._control is not None:
                try:
                    self._control.send(self._envelope('STOP'), secrets.token_hex(8))
                except (BrokenPipeError, ConnectionResetError):
                    pass  # peer already drained; waitpid below still proves exit
                finally:
                    self._control.close()
                    self._control = None
            elif self._channel is not None:
                self._channel.close()
        if self._process is not None:
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False  # actual producer retains custody; never terminate or steal
        if self._spawned is not None:
            try:
                self._spawned.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False  # refused/partly attached shared owner still retains its own custody
        if self._channel is not None:
            self._channel.close()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                return False
        return True

    def is_alive(self):
        if self._owner is not None:
            from hermes_cli import serve_producer_broker as shared
            return shared.owner_alive(self._owner)
        return self._process is not None and self._process.poll() is None
