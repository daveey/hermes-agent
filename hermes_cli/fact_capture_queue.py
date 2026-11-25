"""Bounded capture workers, one per lane, with independently drainable jobs. [daveey]

Every board's and the project tree's native input captures used to queue on the one ``fact-board-capture`` worker.
On the hub (2026-11-24, board facts on) the tree's four selectors, each walking twelve profiles, waited behind the
board views' captures, so a tree cycle outgrew even a 300 s input validity and the projects tree answered 503
``dependency_unavailable``. A lane is a separate worker: the default lane keeps the board captures serialized as
before, and the tree has its own (``fact_tree_startup``), so neither waits behind the other. Captures inside one lane
stay serialized; a lane's worker exits when its queue is empty and restarts on the next submission.
"""
from collections import deque
import threading

DEFAULT_LANE = 'board'
_lock = threading.Lock()
_pending: dict[str, deque] = {}
_workers: dict[str, threading.Thread] = {}


class CaptureJob:
    def __init__(self, callback, on_complete=None):
        self.callback = callback
        self.on_complete = on_complete
        self.done = threading.Event()
        self.started = False
        self.cancelled = False

    def is_alive(self):
        return not self.done.is_set()

    def join(self, timeout=None):
        with _lock:
            if not self.started:
                self.cancelled = True
                self.callback = self.on_complete = None
                self.done.set()
        self.done.wait(timeout)


def submit(callback, lane=DEFAULT_LANE, *, on_complete=None):
    job = CaptureJob(callback, on_complete)
    with _lock:
        pending = _pending.setdefault(lane, deque())
        pending.append(job)
        if lane not in _workers:
            try:
                _start(lane)
            except BaseException:
                pending.remove(job)
                raise
    return job


def _start(lane):
    """Start ``lane``'s worker (caller holds ``_lock``); on failure nothing stays registered."""
    # health: allow HX012 -- each queued callback enters its own saved scope, never the first caller's context
    worker = threading.Thread(target=_run, args=(lane,), name=f'fact-{lane}-capture', daemon=True)
    _workers[lane] = worker
    try:
        worker.start()
    except BaseException:
        del _workers[lane]
        raise


def _run(lane):
    try:
        while True:
            with _lock:
                pending = _pending.get(lane)
                if not pending:
                    _workers.pop(lane, None)
                    return
                job = pending.popleft()
                if job.cancelled:
                    continue
                job.started = True
            try:
                job.callback()
            finally:
                job.callback = None
                job.done.set()
                complete, job.on_complete = job.on_complete, None
                if complete is not None:
                    complete()
    except BaseException:
        # A callback that escapes with a non-Exception (SystemExit, KeyboardInterrupt) ends this worker: release
        # the lane so the next submission starts a fresh one, instead of leaving it registered and dead forever.
        with _lock:
            if _workers.get(lane) is threading.current_thread():
                del _workers[lane]
                if _pending.get(lane):  # jobs queued behind the failed one still get a worker
                    _start(lane)
        raise
