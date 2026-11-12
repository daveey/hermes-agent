"""[daveey] Reload barrier for the tool registry.

A forced plugin rediscovery (``PluginManager.discover_and_load(force=True)``: a mods reload, a
plugin enable, ``reload-plugins``) unloads every plugin registration of one Hermes home and then
registers them again. Each registry operation is locked, the pass as a whole is not: for the
seconds it takes, a tool call on another thread reads a registry with that home's plugin tools
(and guard hooks) missing and answers ``'<tool>' is not a known tool name`` for a name the
session was just given.

The pass marks its home here; tool resolution waits for the mark to clear before it reads, and
re-reads once when a pass overlapped a miss. Waiting happens strictly before a handler is
invoked, so nothing is executed twice. The reloading thread and everything that inherits its
context (plugin loader deadline workers copy it) never wait, and a pass in another home is not
waited on. The wait is bounded: past the bound the caller proceeds on what the registry holds.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from hermes_constants import hermes_home_key

logger = logging.getLogger(__name__)

# Longest a tool call waits for a pass. Observed passes take 3-15 s; a hung one must not hold a turn.
RELOAD_WAIT_SECONDS = 60.0

_cond = threading.Condition()
_active: dict[str, int] = {}   # home key -> passes in flight
_epoch: dict[str, int] = {}    # home key -> passes ever started
# Home keys this context is reloading: set on the reloading thread, inherited by loader workers.
_reloading: ContextVar[frozenset] = ContextVar("hermes_registry_reloading", default=frozenset())


@contextmanager
def reload_barrier(enabled: bool = True) -> Iterator[None]:
    """Mark the current Hermes home as mid-reload for the length of the block."""
    if not enabled:
        yield
        return
    key = hermes_home_key()
    with _cond:
        _active[key] = _active.get(key, 0) + 1
        _epoch[key] = _epoch.get(key, 0) + 1
    token = _reloading.set(_reloading.get() | {key})
    try:
        yield
    finally:
        _reloading.reset(token)
        with _cond:
            remaining = _active.get(key, 1) - 1
            if remaining > 0:
                _active[key] = remaining
            else:
                _active.pop(key, None)
            _cond.notify_all()


def reload_epoch(scope: Optional[str] = None) -> int:
    """Passes started so far for *scope* (default: the current home); compare to detect an overlap."""
    key = hermes_home_key(scope)
    with _cond:
        return _epoch.get(key, 0)


def reload_active(scope: Optional[str] = None) -> bool:
    key = hermes_home_key(scope)
    with _cond:
        return _active.get(key, 0) > 0


def wait_for_reload(scope: Optional[str] = None, timeout: Optional[float] = None) -> bool:
    """Block while another thread reloads *scope*'s plugins. True when a pass was waited on.

    Returns at once when no pass is in flight, or when the caller is (part of) the pass."""
    key = hermes_home_key(scope)
    if key in _reloading.get():
        return False
    deadline = time.monotonic() + (RELOAD_WAIT_SECONDS if timeout is None else timeout)
    waited = False
    with _cond:
        while _active.get(key, 0) > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("plugin reload for %s still running after the wait bound; "
                               "resolving tools against the registry as it is", key)
                return True
            waited = True
            _cond.wait(remaining)
    return waited


def settle_reload_since(epoch: int, scope: Optional[str] = None, timeout: Optional[float] = None) -> bool:
    """True when a pass for *scope* ran or started since *epoch* was read (after waiting it out):
    a miss observed in between is not authoritative and the caller should read again."""
    key = hermes_home_key(scope)
    if key in _reloading.get():
        return False
    waited = wait_for_reload(scope, timeout)
    return waited or reload_epoch(scope) != epoch
