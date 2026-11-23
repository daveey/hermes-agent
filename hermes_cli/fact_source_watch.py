"""One process-wide native owner, with independently scoped source leases. [daveey]

FileEvents owns loss-aware coverage and rearming. Its descriptors are multiplexed
here, rather than unioning paths: unrelated profiles must not all recapture on
every SQLite write. Only the owner touches FileEvents except its wake() method.
"""
from __future__ import annotations

import contextvars
import logging
import queue
import select
import threading
import time

from agent.memory_provider import spawn_context_thread
from hermes_cli.annotation_file_events import FileEvents
from hermes_cli.fact_source_signal import cue_path

_log = logging.getLogger(__name__)
_shared_lock = threading.Lock()
_shared = None


class _Request:
    def __init__(self, action):
        self.action = action
        self.done = threading.Event()
        self.error = None

    def wait(self):
        if not self.done.wait(5.0):
            raise TimeoutError('native source watch did not stop; descriptors retained')
        if self.error is not None:
            raise self.error


class _NativeOwner:
    def __init__(self):
        self._control = FileEvents()
        self._requests = queue.Queue()
        self._watches = set()
        self._retained = []
        self._stopped = False
        try:
            # Infrastructure belongs to the process, not the first subscriber's
            # profile or local-work scope. Each callback binds its own context.
            self._thread = contextvars.Context().run(
                spawn_context_thread, self._run, name='fact-native-watch', passive=True)
            self._thread.start()
        except BaseException:
            self._control.close()
            raise

    def submit(self, action):
        if threading.current_thread() is self._thread:
            raise RuntimeError('native watch callbacks cannot synchronously change leases')
        request = _Request(action)
        self._requests.put(request)
        self._control.wake()
        return request

    def add(self, watch):
        events = FileEvents()
        try:
            events.replace(watch._paths, directories=watch._directories)
        except BaseException:
            # Failed cleanup still has a process-owned retry path.
            self._retained.append(events)
            events.close()
            self._retained.remove(events)
            raise
        watch._events = events
        self._watches.add(watch)

    def remove(self, watch):
        if watch in self._watches:
            watch._events.close()
            self._watches.remove(watch)
        if not self._watches:
            for events in tuple(self._retained):
                events.close()
                self._retained.remove(events)
            self._control.close()
            self._stopped = True

    def _commands(self):
        while not self._requests.empty():
            request = self._requests.get_nowait()
            try:
                request.action()
            except BaseException as exc:  # health: allow BLE001 -- acknowledge the exact owner-command error to its waiting lease
                request.error = exc
            finally:
                request.done.set()

    def _notify(self, watch):
        try:
            watch._context.run(watch._wake)
        except Exception:  # health: allow BLE001 -- one subscriber callback must not kill all native hints
            # A bad consumer must not kill native hints for every other profile.
            _log.exception('native source hint callback failed')

    def _capture_hint(self, watch, *, repair=False):
        try:
            if not repair and not watch._events.wait(0):
                return
            watch._events.replace(watch._paths, directories=watch._directories)
            with watch._lock:
                watch._error = None
            watch._retry_at = None
            watch._retry = 0.1
        except OSError as exc:
            with watch._lock:
                watch._error = exc
            watch._retry_at = time.monotonic() + watch._retry
            watch._retry = min(watch._retry * 2, 5.0)
        self._notify(watch)

    def _run(self):
        while not self._stopped:
            self._commands()
            if self._stopped:
                return
            waiter = select.poll()
            # FileEvents has no public fileno: these are readiness only; reads,
            # replacement and closes remain exclusively on this owner thread.
            waiter.register(self._control._wake_read, select.POLLIN)
            by_fd = {}
            deadlines = []
            for watch in self._watches:
                if watch._retry_at is not None:
                    deadlines.append(watch._retry_at)
                else:
                    for fd in (watch._events._fd, watch._events._wake_read):
                        waiter.register(fd, select.POLLIN)
                        by_fd[fd] = watch
            timeout = None
            if deadlines:
                timeout = max(0, (min(deadlines) - time.monotonic()) * 1000)
            ready = waiter.poll(timeout)
            if any(fd == self._control._wake_read for fd, _ in ready):
                self._control.wait(0)
            changed = {by_fd[fd] for fd, _ in ready if fd in by_fd}
            # Process commands on the next iteration so no removed lease can be
            # called from a stale readiness snapshot after close acknowledges.
            now = time.monotonic()
            for watch in tuple(self._watches):
                repair = watch._retry_at is not None and watch._retry_at <= now
                if repair or watch in changed:
                    self._capture_hint(watch, repair=repair)


class NativeSourceWatch:
    def __init__(self, paths, wake, *, directories=()):
        global _shared
        if _shared is not None and threading.current_thread() is _shared._thread:
            raise RuntimeError('native watch callbacks cannot synchronously change leases')
        paths = tuple(paths)
        self._paths = paths + tuple(cue_path(path) for path in paths)
        self._directories = tuple(directories)
        self._wake = wake
        self._context = contextvars.copy_context()
        self._lock = threading.Lock()
        self._error = None
        self._retry_at = None
        self._retry = 0.1
        self._close_request = None
        self._closed = False
        with _shared_lock:
            if _shared is not None and _shared._stopped:
                self._owner = _shared
                self._finish_owner()
            if _shared is None:
                _shared = _NativeOwner()
            self._owner = _shared
            try:
                # Acknowledged only after coverage exists: bootstrap cannot race
                # registration, including when another source is already live.
                self._owner.submit(lambda: self._owner.add(self)).wait()
            except BaseException:
                cleanup = self._owner.submit(lambda: self._owner.remove(self))
                cleanup.wait()
                self._finish_owner()
                raise

    def check(self):
        with self._lock:
            error = self._error
        if error is not None:
            raise OSError('source watch unavailable') from error

    def _finish_owner(self):
        global _shared
        if self._owner._stopped:
            self._owner._thread.join(5.0)
            if self._owner._thread.is_alive():
                raise TimeoutError('native source watch did not stop; descriptors retained')
            if _shared is self._owner:
                _shared = None

    def close(self):
        if threading.current_thread() is self._owner._thread:
            raise RuntimeError('native watch callbacks cannot synchronously change leases')
        with _shared_lock:
            if self._closed:
                return
            if not self._owner._stopped:
                if self._close_request is None or self._close_request.done.is_set():
                    self._close_request = self._owner.submit(lambda: self._owner.remove(self))
                self._close_request.wait()
            self._finish_owner()
            self._closed = True
