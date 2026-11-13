"""Agent-lifetime seam helpers over the single ``hermes_cli.local_work`` registry.

Thin and stdlib-only: no second registry, no coverage claim. When no registry is installed (CLI, tests that
do not opt in) every helper is a no-op, so seams cost nothing outside a gateway that installed one.
Home is the captured ``ACTIVE_SCOPE`` home when inside tracked work, else the context Hermes home; it is
never discovered from anything else, and an unbound home is the registry's sticky unknown.
"""
from contextlib import contextmanager, nullcontext
import functools
import threading

from hermes_cli import local_work


def registry():
    """The installed registry, or None (never installs lazily)."""
    return local_work._registry


def current_home():
    scope = local_work.ACTIVE_SCOPE.get()
    if scope is not None and scope.home is not None:
        return scope.home
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:  # health: allow BLE001 -- unresolvable home is "no home" (callers latch unknown); must never raise into production paths
        return None


class Held:
    """One reservation whose release is idempotent and callable from any thread."""

    __slots__ = ("_lock", "_registry", "_token")

    def __init__(self, reg, token):
        self._registry = reg
        self._token = token
        self._lock = threading.Lock()

    def release(self):
        with self._lock:
            token, self._token = self._token, None
        if token is None:
            return False
        return self._registry.finish(token)

    @property
    def held(self):
        return self._token is not None

    def mark_unknown(self, reason):
        self._registry.mark_unknown(reason)


def reserve(kind, source, *, home=None):
    """Reserve BEFORE the physical work starts; None when no registry is installed."""
    reg = registry()
    if reg is None:
        return None
    return Held(reg, reg.reserve(kind, home=home if home is not None else current_home(), source=source))


def release(held):
    return held.release() if held is not None else False


def scope(kind, source, *, home=None):
    reg = registry()
    if reg is None:
        return nullcontext()
    return reg.scope(kind, home=home if home is not None else current_home(), source=source)


def scoped(kind, source=None):
    """Method decorator: the whole call is one reservation (also sets ACTIVE_SCOPE for nested work)."""
    def decorate(fn):
        label = source or f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with scope(kind, label):
                return fn(*args, **kwargs)
        return wrapper
    return decorate


def submit(executor, fn, *, kind, source, home=None):
    """``executor.submit(fn)`` with reserve-before-submit; canceled-before-run releases exactly once."""
    reg = registry()
    if reg is None or getattr(executor, "_shutdown", False):
        # A shut-down pool rejects with certainty (nothing enqueued): never reserve for it.
        return executor.submit(fn)
    from hermes_cli.local_work_threads import submit_tracked
    return submit_tracked(executor, fn, registry=reg, home=home if home is not None else current_home(),
                          source=source, kind=kind)
