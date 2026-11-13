"""Seam helpers for local-work coverage of tool/provider/RPC/MCP/plugin entries.

Stdlib + ``hermes_cli.local_work`` only. Every helper is a no-op when no registry
is installed (never installs one lazily). Home ownership is inherited from the
active scope, else the context's Hermes home key; an unbound home latches
``unknown-home`` in the registry rather than being silently mapped.

``Handle`` separates reservation lifetime from caller lifetime: ``finish`` is the
only release, and is idempotent for the holder.
"""
from contextlib import contextmanager, nullcontext
import threading

from hermes_cli import local_work


def _registry():
    try:
        return local_work.get_registry()
    except RuntimeError:
        return None


def _home(registry):
    active = local_work.ACTIVE_SCOPE.get()
    if active is not None and active.registry is registry and active.home is not None:
        return active.home
    try:
        from hermes_constants import hermes_home_key

        return hermes_home_key()
    except Exception:  # health: allow BLE001 -- unresolvable home means unattributed; the registry latches unknown downstream
        return None


class Handle:
    """Single-release custody of one reservation."""

    __slots__ = ("_lock", "_registry", "_token")

    def __init__(self, registry, token):
        self._registry = registry
        self._token = token
        self._lock = threading.Lock()

    def finish(self):
        with self._lock:
            token, self._token = self._token, None
        if token is not None:
            self._registry.finish(token)


def reserve(kind, source):
    """Reserve now; return a Handle (None when no registry is installed)."""
    registry = _registry()
    if registry is None:
        return None
    return Handle(registry, registry.reserve(kind, home=_home(registry), source=source))


def finish(handle):
    if handle is not None:
        handle.finish()


def scope(kind, source, *, home=None):
    """Context manager: reserve on entry (first statement), release on exit.

    ``home`` (a path) overrides inherited ownership, e.g. a plugin host's own profile.
    """
    registry = _registry()
    if registry is None:
        return nullcontext()
    if home is not None:
        try:
            from hermes_constants import hermes_home_key

            home = hermes_home_key(home)
        except Exception:  # health: allow BLE001 -- unresolvable home means unattributed; the registry latches unknown downstream
            home = None
    else:
        home = _home(registry)
    return registry.scope(kind, home=home, source=source)


@contextmanager
def reserved(kind, source):
    handle = reserve(kind, source)
    try:
        yield handle
    finally:
        finish(handle)


def tracked_thread(kind, source, **kwargs):
    """TrackedThread owned by the current scope/home; plain Thread w/o registry."""
    registry = _registry()
    if registry is None:
        return threading.Thread(**kwargs)  # health: allow HX012 -- no registry installed: plain thread, nothing to track
    from hermes_cli.local_work_threads import TrackedThread, thread_class_replaced

    if thread_class_replaced(registry):
        return threading.Thread(**kwargs)  # health: allow HX012 -- replaced class: honoured, unknown coverage latched
    return TrackedThread(registry=registry, home=_home(registry), source=source,
                         kind=kind, **kwargs)


def submit(executor, function, /, *args, kind, source):
    """``submit_tracked`` with inherited ownership; ``None`` when no registry is installed.

    Reservation precedes enqueue and is released by the target's own finally (or by a
    verified never-started cancel), never by a canceled/abandoned waiter.
    """
    registry = _registry()
    if registry is None:
        return None
    from hermes_cli.local_work_threads import submit_tracked

    if not hasattr(executor, "submit"):  # lazy factory: build the pool only when tracking
        executor = executor()

    return submit_tracked(executor, function, *args, registry=registry,
                          home=_home(registry), source=source, kind=kind)
