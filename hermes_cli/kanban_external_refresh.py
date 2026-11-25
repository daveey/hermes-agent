"""Serialize overlapping external-display captures by board, without retaining DB handles."""
from contextlib import contextmanager
import threading

_lock = threading.Lock()
_active: dict[str, tuple[threading.Lock, int]] = {}


@contextmanager
def board_refresh(path):
    """Keep lock identity until the last waiter leaves; unrelated boards do not block."""
    key = str(path)
    with _lock:
        lock, users = _active.get(key, (threading.Lock(), 0))
        _active[key] = (lock, users + 1)
    try:
        with lock:
            yield
    finally:
        with _lock:
            _, users = _active[key]
            if users == 1:
                del _active[key]
            else:
                _active[key] = (lock, users - 1)
