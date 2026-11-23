"""Admission fence for authorized legacy board paths during coordinated cutover.

Paths are supplied by startup, never resolved here. Closing admission precedes
waiting, so a late read cannot reopen an observer after it has been retired.
"""
from __future__ import annotations

import functools
import inspect
import threading
from contextlib import contextmanager


class LegacyStopped(RuntimeError):
    """The release latch withdrew this path's legacy reader."""


_condition = threading.Condition()
_blocked: set[str] = set()
_active: dict[str, int] = {}
_local = threading.local()


def reserve(path):
    """Reserve before dispatching a background job; release in its finally."""
    path = str(path)
    with _condition:
        if path in _blocked:
            raise LegacyStopped("legacy board reader stopped")
        _active[path] = _active.get(path, 0) + 1
    return path


def release(path):
    with _condition:
        remaining = _active[path] - 1
        if remaining:
            _active[path] = remaining
        else:
            del _active[path]
        _condition.notify_all()


@contextmanager
def borrowed(path):
    """Nested synchronous readers belong to the already-admitted operation."""
    path = str(path)
    depths = getattr(_local, "depths", None)
    if depths is None:
        depths = _local.depths = {}
    outer = path not in depths
    if outer:
        reserve(path)
    depths[path] = depths.get(path, 0) + 1
    try:
        yield
    finally:
        depths[path] -= 1
        if not depths[path]:
            del depths[path]
        if outer:
            release(path)


def reader(path_parameter="path"):
    """Fence the whole reader, including fallback and cache publication."""
    def decorate(function):
        signature = inspect.signature(function)

        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs).arguments
            path = arguments[path_parameter]
            if path_parameter == "key":
                path = path[0] if path is not None else None
            if path is None:
                return function(*args, **kwargs)
            with borrowed(path):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def stop(paths, *, timeout=5.0):
    """Off-request drain. Failure leaves admission closed until explicit rollback."""
    paths = frozenset(map(str, paths))
    with _condition:
        _blocked.update(paths)
        if not _condition.wait_for(lambda: not any(p in _active for p in paths), timeout):
            raise TimeoutError("legacy board readers did not drain")


def resume(paths):
    """Called only after stop has drained and legacy caches were retired."""
    with _condition:
        _blocked.difference_update(map(str, paths))
