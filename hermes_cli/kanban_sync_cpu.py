"""Same-thread local/Board CPU partition for completed sync services. [daveey]

Only Hub._call is excluded: JSON encode/decode, HTTP/TLS and its retries. Local
row work and attachment preparation remain local. Detached transfer/feed/metrics
threads are outside this envelope, never subtracted from the caller's CPU.

The loop scope includes discovery, maintenance and wait_for_work CPU. Board scopes
are nested subsets, NOT additive. All three counters use the same completed-service
boundary; readers reconciling snapshots must require equal runs (record publishes
separately). The local counter alone is a directly measured exclusive CPU total.
"""
from contextlib import ExitStack, contextmanager
from functools import wraps
import os
import threading
import time

from hermes_cli import loop_accounting

_local = threading.local()


@contextmanager
def service(name):
    """Publish one completed service, including exceptional exits, on this thread."""
    frames = getattr(_local, "frames", ())
    frame = {"board": 0.0}
    _local.frames = (*frames, frame)
    pid = os.getpid()
    start = time.thread_time()
    error = None
    try:
        yield
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        total = time.thread_time() - start
        _local.frames = frames
        # Never combine pre-fork thread CPU with the child's clock.
        if os.getpid() == pid:
            excluded = min(total, frame["board"])
            for kind, cpu in (("local", total - excluded), ("board_call", excluded), ("total", total)):
                loop_accounting.record(f"sync.cpu.{kind}/{name}", cpu_s=cpu,
                                       error=error is not None, error_kind=error, outer=False)


def board_call(fn):
    """Exclude one outermost synchronous Board call from active services only."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        frames = getattr(_local, "frames", ())
        if not frames or getattr(_local, "in_call", False):
            return fn(*args, **kwargs)
        _local.in_call = True
        pid = os.getpid()
        start = time.thread_time()
        try:
            return fn(*args, **kwargs)
        finally:
            cpu = time.thread_time() - start
            _local.in_call = False
            if os.getpid() == pid:
                for frame in frames:
                    frame["board"] += cpu
    return wrapped


@contextmanager
def iterations(keep_running):
    """Own iteration services in the caller's scope, including its bounded wait.

    The stack sees the caller's real exit, not GeneratorExit from closing a
    suspended generator. Completed iterations publish before the next predicate.
    """
    with ExitStack() as scope:
        def rounds():
            while keep_running():
                if loop_accounting.scopes_enabled():
                    scope.enter_context(loop_accounting.Scope("sync.round/loop"))
                scope.enter_context(service("loop"))
                yield
                scope.close()
        yield rounds()
