"""Content-free in-process process lifecycle hints. [daveey]

Only wake a subscribed owner; never snapshot a registry or carry commands, PIDs,
profile identities or execution authority. Checkpoint admission also covers
sandboxed jobs whose registry mutations intentionally have no host file write.
"""
import logging
import threading

_lock = threading.Lock()
_listeners = {}
_log = logging.getLogger(__name__)


def subscribe(registry, wake):
    token = object()
    with _lock:
        _listeners[token] = (registry, wake)
    def close():
        with _lock:
            _listeners.pop(token, None)
    return close


def publish(registry):
    with _lock:
        callbacks = [wake for owner, wake in _listeners.values() if owner is registry]
    for wake in callbacks:
        try:
            wake()
        except Exception:  # health: allow BLE001 -- a failed hint cannot break process completion or peer wakes
            _log.exception('process lifecycle subscriber failed')
