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

from hermes_cli import loop_accounting as _la
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
        return self._request({'name': name, 'selector': selector})

    def capture_many(self, views):
        started = time.monotonic()
        if not 1 <= len(views) <= 4:
            raise CaptureUnavailable('request_views')
        result = json.loads(self._request({'views': views}))
        results = result.get('results')
        if not isinstance(results, list) or len(results) != len(views):
            raise CaptureUnavailable('invalid_body')
        bodies = []
        for item in results:
            if not isinstance(item, dict):
                raise CaptureUnavailable('invalid_body')
            if set(item) == {'body'} and isinstance(item['body'], dict):
                bodies.append(json.dumps(item['body'], separators=(',', ':'), allow_nan=False).encode())
            elif item == {'unavailable': 'reader_error'}:
                bodies.append(CaptureUnavailable('reader_error'))
            else:
                raise CaptureUnavailable('invalid_body')
        if time.monotonic() - started > self.seconds:
            raise CaptureUnavailable('timeout')
        return bodies

    def _request(self, value):
        started = time.monotonic()
        deadline = started + self.seconds
        request = json.dumps(value, allow_nan=False).encode('utf-8')
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
        cpu_s, completed = None, False
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
            cpu_s = _child_cpu(child.pid)  # before the wait reaps it and its counters
            if child.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                raise CaptureUnavailable('crash')
            if time.monotonic() > deadline:
                raise CaptureUnavailable('timeout')
            result = json.loads(body)
            if not isinstance(result, dict):
                raise CaptureUnavailable('invalid_body')
            completed = True
            return body
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise CaptureUnavailable('transport') from exc
        finally:
            if cpu_s is None:
                cpu_s = _child_cpu(child.pid)  # a failed or cancelled child spent CPU too
            # The child's CPU is outside this process: no thread here may claim it (outer=False).
            _la.record('sessions.capture.child', cpu_s=cpu_s or 0.0, wall_s=time.monotonic() - started,
                       fallback=cpu_s is None, error=not completed,
                       error_kind=None if completed else 'CaptureUnavailable', outer=False)
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


def _child_cpu(pid):
    """CPU seconds of a live or not-yet-reaped child plus the descendants it has reaped, else None.

    Linux procfs only, like the native capture itself. Sampled when the response frame ends, so
    it leaves out the child's interpreter teardown: a lower bound, never an estimate.
    """
    try:
        with open(f'/proc/{pid}/stat', encoding='ascii', errors='replace') as stat:
            fields = stat.read().rpartition(')')[2].split()
        return sum(int(fields[index]) for index in (11, 12, 13, 14)) / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError):
        return None
