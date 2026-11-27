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
from hermes_cli.git_fingerprint import read_git_revision_fingerprint
from hermes_cli.process_identity import spawn_owned, stop_owned_group_survivors
from tools.environments.local import build_subprocess_env

MAX_REQUEST = 16 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
# A standby older than this is not used: it imported its code, and leased its dependencies, that long ago.
STANDBY_MAX_AGE_S = 30


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


def _generation():
    """Checkout identity, as the code-skew watchdog reads it (``.git`` HEAD/ref files, no git spawn)."""
    return read_git_revision_fingerprint(Path(project_root_str()))


class _Standby:
    __slots__ = ('accounted', 'child', 'cpu_s', 'generation', 'retiring', 'spawned')

    def __init__(self, child, spawned):
        self.child, self.spawned, self.generation, self.cpu_s = child, spawned, None, None
        # Retirement can be entered twice: by two threads, or by a signal handler's close() on the
        # thread already retiring it. ``retiring`` makes other threads wait and lets the same thread
        # through; ``accounted`` is a lock used as an atomic flag (taken once, never blocked on).
        self.retiring, self.accounted = threading.RLock(), threading.Lock()


class CaptureProcess:
    """One active child at a time; close cancels it, and a later capture gets a fresh child.

    ``prepare`` may spawn one standby ahead of the first capture; it serves at most one request.

    ``prepare`` and ``close`` run on the main thread at boot and shutdown, where a SIGTERM/SIGINT
    handler may call ``close`` in the middle of either. So neither holds ``_lock`` across a spawn or
    a child stop, and the lock is re-entrant: a handler arriving inside one of their short locked
    sections runs a whole ``close`` there instead of waiting for the thread it interrupted.
    """
    def __init__(self, *, seconds=5, max_bytes=MAX_RESPONSE, command=None):
        self.seconds, self.max_bytes = seconds, min(max_bytes, MAX_RESPONSE)
        self.command = command
        self._lock = threading.RLock()
        self._child = None
        self._standby = None
        self._closed = False

    def _spawn(self):
        # Resolve installation-bound Python/dependencies, not ambient python/PYTHONPATH.
        command = self.command or runtime_command(Path(project_root_str()),
                                                   module='hermes_cli.sessions_capture_worker')
        return spawn_owned(command, purpose='sessions-canonical-capture',
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           env=build_subprocess_env())

    def prepare(self):
        """Spawn the standby the next capture will use (it imports while it waits for the request).
        No-op when closed, busy or already prepared; a failed spawn leaves captures spawning fresh."""
        with self._lock:
            if self._closed or self._standby is not None or self._child is not None:
                return
        # Spawned with the lock free (see the class docstring); what happened meanwhile is settled below.
        spawned = time.monotonic()
        try:
            child = self._spawn()
        except OSError:
            return
        standby, kept = _Standby(child, spawned), False
        try:
            standby.generation = _generation()
            with self._lock:
                if self._standby is None and self._child is None:  # a capture may have started meanwhile
                    self._standby = standby
            # Published BEFORE _closed is read, while close() sets _closed before it reads _standby:
            # whichever way a close() interleaved, it saw this standby or this line sees it closed.
            kept = self._standby is standby and not self._closed
        finally:
            if not kept:
                self._retire(standby)

    def _retire(self, standby):
        """Retire an unused standby: one ``skipped`` record, then the same custody as any child.
        A group that does not stop keeps the standby owned for the next try. Safe to enter again
        while it runs (see ``_Standby``): the second entry finishes the stop and records nothing."""
        with standby.retiring:
            if standby.accounted.acquire(blocking=False):
                # A standby poll() already reaped lost its counters: use the sample taken before that.
                cpu_s = _child_cpu(standby.child.pid) if standby.child.returncode is None else standby.cpu_s
                _la.record('sessions.capture.child', cpu_s=cpu_s or 0.0, wall_s=time.monotonic() - standby.spawned,
                           skipped=True, fallback=cpu_s is None, outer=False)
            _stop(standby.child)
        with self._lock:
            if self._standby is standby:
                self._standby = None

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
        with self._lock:
            if self._closed:
                raise CaptureUnavailable('closed')
            if self._child is not None:
                raise CaptureUnavailable('busy')
            child, standby = None, self._standby
            if standby is not None:
                standby.cpu_s = _child_cpu(standby.child.pid)  # before poll() can reap a dead one
                if (standby.child.poll() is None and time.monotonic() - standby.spawned <= STANDBY_MAX_AGE_S
                        and standby.generation == _generation()):
                    child, self._standby = standby.child, None
                else:
                    self._retire(standby)
            if child is None:
                try:
                    child = self._spawn()
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
            # Lifetime CPU, so a used standby's imports count; wall is this request's (the batch charge).
            _la.record('sessions.capture.child', cpu_s=cpu_s or 0.0, wall_s=time.monotonic() - started,
                       fallback=cpu_s is None, error=not completed,
                       error_kind=None if completed else 'CaptureUnavailable', outer=False)
            _stop(child)
            with self._lock:
                self._child = None

    def close(self):
        with self._lock:
            self._closed = True
            child, standby = self._child, self._standby
        try:
            if child is not None:  # its own request accounts it
                if child.poll() is None:
                    kill_process_tree(child)
                child.wait(timeout=1)
                if not stop_owned_group_survivors(child.pid, proc=child, grace=0):
                    raise TimeoutError('sessions child group did not stop; custody retained')
                with self._lock:
                    if self._child is child:
                        self._child = None
        finally:
            # Attempted even when the active child's custody is retained. Outside the lock: a signal
            # handler's close() arriving mid-stop then finishes this same retirement instead of blocking.
            if standby is not None:
                self._retire(standby)


def _stop(child):
    if child.poll() is None:
        kill_process_tree(child)
    child.wait(timeout=1)
    child.stdout.close()
    if not child.stdin.closed:
        child.stdin.close()
    if not stop_owned_group_survivors(child.pid, proc=child, grace=0):
        raise TimeoutError('sessions child group did not stop; custody retained')


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
