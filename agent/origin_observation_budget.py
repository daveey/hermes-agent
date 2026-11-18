"""Optional native-origin reads must not monopolize a board writer.

Capture warms the owner link before the transaction. Record never reconnects and
never uses an earlier authority result. This budget isn't a persistence timeout.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import socket
import threading
import time

_deadline: ContextVar[float | None] = ContextVar('origin_observation_deadline', default=None)
RECORD_TIMEOUT = 0.5


def remaining():
    deadline = _deadline.get()
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError('origin_observation_timeout')
    return left


def bounded_record(fn):
    @wraps(fn)
    def bounded(*args, **kwargs):
        prior = _deadline.get()
        deadline = time.monotonic() + RECORD_TIMEOUT
        token = _deadline.set(min(prior, deadline) if prior is not None else deadline)
        try:
            return fn(*args, **kwargs)
        except TimeoutError:
            return None
        finally:
            _deadline.reset(token)
    return bounded


@contextmanager
def observation_lock(lock):
    left = remaining()
    acquired = lock.acquire() if left is None else lock.acquire(timeout=left)
    if not acquired:
        raise TimeoutError('origin_observation_timeout')
    try:
        remaining()
        yield
    finally:
        lock.release()


@contextmanager
def interrupt_at_deadline(ws):
    """Interrupt socket writes too, without waiting for a close handshake.

    Use shutdown rather than websocket.close (which can wait on its own writer).
    Join the timer before returning: no abandoned helper can close a reused link.
    """
    sock = ws.socket

    def interrupt():
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already closed

    left = remaining()
    if left is None:
        raise RuntimeError('missing_origin_deadline')
    timer = threading.Timer(left, interrupt)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        timer.join()
    remaining()
