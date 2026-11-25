"""Execution-scoped provider exclusions, applied before credential selection/allocation."""
from contextlib import contextmanager
from contextvars import ContextVar

_EXCLUDED = ContextVar('execution_provider_exclusions', default=frozenset())


def provider_allowed(provider):
    return str(provider or '').strip().lower() not in _EXCLUDED.get()


@contextmanager
def exclude_providers(providers):
    """Compose restrictions; a nested caller cannot relax its parent's exclusion set."""
    token = _EXCLUDED.set(_EXCLUDED.get() | frozenset(str(p).strip().lower() for p in providers))
    try:
        yield
    finally:
        _EXCLUDED.reset(token)
