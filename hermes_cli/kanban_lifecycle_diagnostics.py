"""Cheap lifecycle ownership; sample symbolic stacks only after a timed-out wait.

No logging, file reads or source/locals capture occurs under the board lock.
Snapshots are best effort: a holder may finish as its waiter times out.
"""
from __future__ import annotations

import os
import sys
import threading
import time


class LifecycleLock:
    """The board's reentrant transition lock, including raw connection-close users."""

    def __init__(self):
        self._lock = threading.RLock()
        self._owner = None
        self._depth = 0

    def acquire(self, blocking: bool = True, timeout: float = -1):
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            if self._depth == 0:
                self._owner = (threading.get_ident(), time.monotonic())
            self._depth += 1
        return acquired

    def release(self):
        # Do not corrupt diagnostics if a foreign/unbalanced release is rejected.
        if self._owner is None or self._owner[0] != threading.get_ident():
            raise RuntimeError("cannot release un-acquired lock")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()

    def snapshot(self):
        owner = self._owner
        if owner is None:
            return {}
        ident, started = owner
        stack = []
        frame = sys._current_frames().get(ident)
        try:
            while frame is not None and len(stack) < 16:
                code = frame.f_code
                stack.append(f"{os.path.basename(code.co_filename)}:{frame.f_lineno}:{code.co_name}")
                frame = frame.f_back
        finally:
            del frame
        # Never attribute a previous owner's stack to a replacement owner.
        if self._owner is not owner:
            return {}
        return {"holder_thread": ident, "held_ms": round((time.monotonic() - started) * 1000, 3),
                "holder_stack": stack}


def busy_details(exc):
    """No exception message/SQL/locals: only type, code and captured lock metadata."""
    return (type(exc).__name__, getattr(exc, "sqlite_errorcode", None), os.getpid(),
            getattr(exc, "diagnostic", {}))
