"""One bounded board-capture worker, with independently drainable jobs. [daveey]"""
from collections import deque
import threading

_lock = threading.Lock()
_pending = deque()
_worker = None


class CaptureJob:
    def __init__(self, callback):
        self.callback = callback
        self.done = threading.Event()
        self.started = False
        self.cancelled = False

    def is_alive(self):
        return not self.done.is_set()

    def join(self, timeout=None):
        with _lock:
            if not self.started:
                self.cancelled = True
                self.callback = None
                self.done.set()
        self.done.wait(timeout)


def submit(callback):
    global _worker
    job = CaptureJob(callback)
    with _lock:
        _pending.append(job)
        if _worker is None:
            # health: allow HX012 -- each queued callback enters its own saved scope, never the first caller's context
            _worker = threading.Thread(target=_run, name='fact-board-capture', daemon=True)
            try:
                _worker.start()
            except BaseException:
                _worker = None
                _pending.remove(job)
                raise
    return job


def _run():
    global _worker
    while True:
        with _lock:
            if not _pending:
                _worker = None
                return
            job = _pending.popleft()
            if job.cancelled:
                continue
            job.started = True
        try:
            job.callback()
        finally:
            job.callback = None
            job.done.set()
