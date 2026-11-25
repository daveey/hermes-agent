"""Bounded waiting-evidence readers, independent of generic dashboard eviction.

Reuse the ordinary cached_ro admission, attestation and statement budgets. Only
retention ownership differs; no output or SQLite snapshot is cached here.
"""
import atexit
import contextlib
import threading
from collections import OrderedDict
from pathlib import Path

from hermes_cli import kanban_generation as generation

LIMIT = 32
_cache = OrderedDict()
_lock = threading.RLock()
# Waiting declarations are already serialized. Preserve that custody across
# admission and cap checks, without taking it from generation-recovery callbacks.
_read_lock = threading.Lock()


def _retire(key, entry, *, gone=False):
    from hermes_cli.kanban_sync_board_watch import path_gone
    if not entry[2].acquire(blocking=False):
        return False
    try:
        with generation.lifecycle_access(key, timeout=0):
            if gone and not path_gone(key):
                return False
            with generation._cache_registry_available(_lock):
                if _cache.get(key) is not entry or entry[0].in_transaction:
                    return False
                del _cache[key]
            entry[0].close()
        return True
    except (generation.BoardLifecycleBusy, generation.DeletedWalGenerationError):
        return False
    finally:
        entry[2].release()


def evict(path):
    """Recovery retains exclusive borrower and actual descriptor-close custody."""
    with generation._cache_registry_available(_lock):
        entries = [(key, entry) for key, entry in _cache.items() if generation._resolved(key) == path]
    for key, entry in entries:
        if not _retire(key, entry):
            raise generation.DeletedWalGenerationError('waiting reader is borrowed; retry recovery')


generation.register_reader_cache(evict)


def close_all():
    with _lock:
        entries = list(_cache.items())
    for key, entry in entries:
        _retire(key, entry)


def release_gone():
    from hermes_cli.kanban_sync_board_watch import path_gone
    with _lock:
        entries = list(_cache.items())
    return sum(_retire(key, entry, gone=True) for key, entry in entries if path_gone(key))


atexit.register(close_all)


@contextlib.contextmanager
def reading(path, *, busy_budget=0.5):
    from hermes_cli import kanban_session_activity as activity
    path = Path(path)
    # No new Python wait for a speculative zero-budget caller. A concurrent or
    # nested declaration uses unchanged private admission, not another cache slot.
    if not _read_lock.acquire(blocking=False):
        with activity._private_ro(path, min(busy_budget, activity._RO_BUSY_TIMEOUT_S)) as conn:
            yield conn
        return
    try:
        key = str(path)
        with _lock:
            candidates = list(_cache.items()) if key not in _cache and len(_cache) >= LIMIT else []
        for old_key, entry in candidates:
            if _retire(old_key, entry):
                break
        with _lock:
            full = key not in _cache and len(_cache) >= LIMIT
        if full:
            with activity._private_ro(path, min(busy_budget, activity._RO_BUSY_TIMEOUT_S)) as conn:
                yield conn
            return
        with activity.cached_ro(path, busy_budget=busy_budget, reader_cache=(_cache, _lock)) as conn:
            with _lock:
                if key in _cache:
                    _cache.move_to_end(key)
            yield conn
    finally:
        _read_lock.release()
