"""Bounded, owner-custodied canonical sessions capture outside the serve GIL."""
from __future__ import annotations


import json
import os
from pathlib import Path
import selectors
import struct
import subprocess
import threading
import time

from hermes_cli._launchers import runtime_command
from hermes_cli._startup_fast import project_root_str
from hermes_cli._subprocess_compat import kill_process_tree
from hermes_cli.process_identity import spawn_owned, stop_owned_group_survivors
from tools.environments.local import build_subprocess_env

MAX_REQUEST = 16 * 1024
MAX_RESPONSE = 4 * 1024 * 1024


class CaptureUnavailable(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _wait(fd, event, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CaptureUnavailable('timeout')
    with selectors.DefaultSelector() as poller:
        poller.register(fd, event)
        if not poller.select(remaining):
            raise CaptureUnavailable('timeout')


def _read_exact(fd, count, deadline):
    data = bytearray()
    while len(data) < count:
        _wait(fd, selectors.EVENT_READ, deadline)
        chunk = os.read(fd, min(65536, count - len(data)))
        if not chunk:
            raise CaptureUnavailable('crash_or_truncated_frame')
        data.extend(chunk)
    return bytes(data)


class CaptureProcess:
    """One child at a time; close cancels it, and a later capture gets a fresh child."""
    def __init__(self, *, seconds=5, max_bytes=MAX_RESPONSE, command=None):
        self.seconds, self.max_bytes = seconds, min(max_bytes, MAX_RESPONSE)
        self.command = command
        self._lock = threading.Lock()
        self._child = None
        self._closed = False

    def __call__(self, name, selector):
        started = time.monotonic()
        deadline = started + self.seconds
        request = json.dumps({'name': name, 'selector': selector}, allow_nan=False).encode('utf-8')
        if len(request) > MAX_REQUEST:
            raise CaptureUnavailable('request_bytes')
        # Resolve installation-bound Python/dependencies, not ambient python/PYTHONPATH.
        command = self.command or runtime_command(Path(project_root_str()),
                                                   module='hermes_cli.sessions_capture_worker')
        with self._lock:
            if self._closed:
                raise CaptureUnavailable('closed')
            if self._child is not None:
                raise CaptureUnavailable('busy')
            try:
                child = spawn_owned(command, purpose='sessions-canonical-capture',
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    env=build_subprocess_env())
            except OSError as exc:
                raise CaptureUnavailable('spawn') from exc
            self._child = child
        try:
            # Small request fits the pipe; a non-reading child must not block the owner.
            os.set_blocking(child.stdin.fileno(), False)
            wire = struct.pack('!I', len(request)) + request
            offset = 0
            while offset < len(wire):
                _wait(child.stdin, selectors.EVENT_WRITE, deadline)
                offset += os.write(child.stdin.fileno(), wire[offset:])
            child.stdin.close()
            size = struct.unpack('!I', _read_exact(child.stdout.fileno(), 4, deadline))[0]
            if size > self.max_bytes:
                raise CaptureUnavailable('response_bytes')
            body = _read_exact(child.stdout.fileno(), size, deadline)
            # Require exactly one frame and a successful child exit, within the SAME budget.
            if _read_trailer(child, deadline):
                raise CaptureUnavailable('trailing_frame')
            if child.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                raise CaptureUnavailable('crash')
            if time.monotonic() > deadline:
                raise CaptureUnavailable('timeout')
            result = json.loads(body)
            if not isinstance(result, dict):
                raise CaptureUnavailable('invalid_body')
            return body
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise CaptureUnavailable('transport') from exc
        finally:
            if child.poll() is None:
                kill_process_tree(child)
            child.wait(timeout=1)
            child.stdout.close()
            if not child.stdin.closed:
                child.stdin.close()
            if not stop_owned_group_survivors(child.pid, proc=child, grace=0):
                raise TimeoutError('sessions child group did not stop; custody retained')
            with self._lock:
                self._child = None

    def close(self):
        with self._lock:
            self._closed = True
            child = self._child
        if child is not None:
            if child.poll() is None:
                kill_process_tree(child)
            child.wait(timeout=1)
            if not stop_owned_group_survivors(child.pid, proc=child, grace=0):
                raise TimeoutError('sessions child group did not stop; custody retained')
            with self._lock:
                if self._child is child:
                    self._child = None


def _read_trailer(child, deadline):
    _wait(child.stdout, selectors.EVENT_READ, deadline)
    return os.read(child.stdout.fileno(), 1)
