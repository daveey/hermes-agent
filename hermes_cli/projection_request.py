"""Consumer request-local binding custody, inherited by detached async workers."""
from contextlib import contextmanager
from contextvars import ContextVar


class RequestBinding:
    """Pin registry resolution, including unbound legacy, until the request exits.

    Immutable context values prevent concurrent request mutation; asyncio.to_thread
    and copied worker contexts retain the selected binding after waiter cancellation.
    The bound adapter still owns mode admission and reader draining.
    """

    def __init__(self, name):
        self._current = ContextVar(name, default=None)

    def get(self, key, installed):
        current = self._current.get()
        if current is not None and key in current[0]:
            return current[1]
        if installed is None:
            return None
        v1, provider = installed
        return v1, provider(key)

    @contextmanager
    def pin(self, key, installed):
        selected = self.get(key, installed)
        token = self._current.set(((key,), selected))
        try:
            yield selected
        finally:
            self._current.reset(token)

    @contextmanager
    def alias(self, key):
        """Add a canonical key only after the caller validates its bound scope."""
        keys, selected = self._current.get()
        token = self._current.set(((*keys, key), selected))
        try:
            yield
        finally:
            self._current.reset(token)
