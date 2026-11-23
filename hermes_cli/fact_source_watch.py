"""Native source hints, armed before bootstrap and rearmed before capture. [daveey]

Linux uses the existing loss-aware inotify owner. Unsupported native backends
fail registration rather than silently polling. Capture retries and watch-repair
retries are failure recovery, not periodic source probes.
"""
from __future__ import annotations

import threading

from agent.memory_provider import spawn_context_thread
from hermes_cli.annotation_file_events import FileEvents
from hermes_cli.fact_source_signal import cue_path


class NativeSourceWatch:
    def __init__(self, paths, wake, *, directories=()):
        paths = tuple(paths)
        self._paths = paths + tuple(cue_path(path) for path in paths)
        self._directories = tuple(directories)
        self._wake = wake
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._error = None
        self._events = FileEvents()
        try:
            # The initial watch exists before registration lets capture start.
            self._events.replace(self._paths, directories=self._directories)
            self._thread = spawn_context_thread(self._run, name="fact-native-watch")
            self._thread.start()
        except BaseException:
            self._events.close()
            raise

    def check(self):
        with self._lock:
            error = self._error
        if error is not None:
            raise OSError("source watch unavailable") from error

    def _run(self):
        retry = 0.1
        repair = False
        while not self._stop.is_set():
            try:
                if not repair and not self._events.wait():
                    continue
                if self._stop.is_set():
                    return
                self._events.replace(self._paths, directories=self._directories)
                with self._lock:
                    self._error = None
                repair = False
                retry = 0.1
                self._wake()
            except OSError as exc:
                with self._lock:
                    self._error = exc
                self._wake()
                repair = True
                if self._stop.wait(retry):
                    return
                retry = min(retry * 2, 5.0)

    def close(self):
        self._stop.set()
        self._events.wake()
        self._thread.join(5.0)
        if self._thread.is_alive():
            raise TimeoutError("native source watch did not stop; descriptors retained")
        self._events.close()
