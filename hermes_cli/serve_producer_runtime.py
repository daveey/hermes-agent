"""Dormant walking skeleton for exec producers, with an in-memory HTTP relay.

No production launch entrypoint exists here. Fixture startup is deliberately
separate from future strict COMPLETE admission. One client and one family are
exercised before shared broker enrollment and the public provider API are added.
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


def projection_response(relay, body):
    """Shared prepared-wire adapter; no source capture or IPC on HTTP threads."""
    from starlette.responses import Response

    try:
        key = relay.submit({'body': body, 'accepted': time.monotonic()})
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


def spawn(role, config, *, custody_fd=None):
    """Fixture exec path; no generic Hermes/plugin/model bootstrap in a child."""
    from hermes_cli._startup_fast import project_root_str
    from tools.environments.local import build_subprocess_env

    parent, child = socket.socketpair()
    script = Path(__file__).with_name('serve_producer_process.py')
    inherited = (child.fileno(),) + (() if custody_fd is None else (custody_fd,))
    command = [sys.executable, '-I', str(script), role, str(child.fileno()), project_root_str()]
    if custody_fd is not None:
        command.append(str(custody_fd))
    try:
        process = subprocess.Popen(command, pass_fds=inherited,
            env=build_subprocess_env(extra={'HERMES_HOME': config['home']}),
            stdin=subprocess.DEVNULL)
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

    def __init__(self, provider, *, home, runtime, generation):
        self.config = {'provider': provider, 'home': str(home), 'runtime': str(runtime),
                       'generation': generation, 'capability': secrets.token_hex(32)}
        self._lock = threading.Lock()
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

    @property
    def broker_pid(self):
        return self._process.pid if self._process else None

    @property
    def pending_count(self):
        with self._lock:
            return len(self._pending)

    def start_for_fixture(self, *, ready_timeout=10):
        # Until launch enrollment is wired there is NO production activation API.
        if 'PYTEST_CURRENT_TEST' not in os.environ:
            raise RuntimeError('strict production admission not integrated')
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

    def submit(self, payload):
        identity = payload.get('body', payload)
        key = hashlib.sha256(b''.join(encode_request('budget', identity))).hexdigest()
        encoded = b''.join(encode_request('x' * 32, self._envelope('PRODUCE', key=key, payload=payload)))
        payload = copy.deepcopy(payload)  # bounded/JSON-validated before retention
        size = len(encoded)
        with self._lock:
            if self._stop.is_set():
                raise ResourceExhausted('relay draining')
            if key not in self._pending:
                if len(self._pending) >= MAX_KEYS or self._bytes + size > MAX_BYTES:
                    raise ResourceExhausted('relay mailbox full')
                self._pending[key] = (payload, size)
                self._bytes += size
        self._wake.set()
        return key

    def read(self, key):
        with self._lock:
            if self._stop.is_set():
                return None
            value = self._outputs.get(key)
            return value[0] if value and value[1] > time.monotonic() else None

    def acknowledged(self, key):
        with self._lock:
            return not self._stop.is_set() and key in self._outputs and key not in self._pending

    def _envelope(self, verb, **extra):
        return dict(capability=self.config['capability'], generation=self.config['generation'], verb=verb, **extra)

    def _run(self):
        try:
            while not self._stop.is_set():
                self._wake.wait()
                self._wake.clear()
                while not self._stop.is_set():
                    with self._lock:
                        item = next(iter(self._pending.items()), None)
                    if item is None:
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
                        self._pending.pop(key)
                        self._bytes -= size
                        if result.get('verb') == 'PUBLISHED':
                            wire = result['wire'].encode('utf-8')
                            self._outputs[key] = (wire, time.monotonic() + 5)
                            while len(self._outputs) > MAX_KEYS or sum(len(v[0]) for v in self._outputs.values()) > MAX_BYTES:
                                self._outputs.popitem(last=False)
                        else:
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

    def close(self, *, timeout: float = 10):
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
        if self._channel is not None:
            self._channel.close()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                return False
        return True

    def is_alive(self):
        return self._process is not None and self._process.poll() is None
