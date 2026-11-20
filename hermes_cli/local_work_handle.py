"""Inline/task reservations: updater accounting and independent drain custody."""
from contextlib import contextmanager
import threading

from hermes_cli.local_work import ACTIVE_SCOPE, WorkScope


class WorkHandle:
    """Own both reservations until actual completion; finish is idempotent."""

    def __init__(self, registry, kind, home, source, *, drain=None):
        self.drain = drain
        self.owner = WorkScope(registry, kind, home, source) if registry is not None else None
        self.token = registry.reserve(kind, home=home, source=source) if registry is not None else None
        self._lock = threading.Lock()
        self._done = False

    @contextmanager
    def bind(self):
        from hermes_cli.worker_drain_fence import lease_scope

        token = ACTIVE_SCOPE.set(self.owner) if self.owner is not None else None
        try:
            if self.drain is not None:
                with lease_scope(self.drain):
                    yield
            else:
                yield
        finally:
            if token is not None:
                ACTIVE_SCOPE.reset(token)

    def finish(self, _unused=None):
        with self._lock:
            if self._done:
                return False
            self._done = True
        try:
            return self.owner.registry.finish(self.token) if self.owner is not None else True
        finally:
            if self.drain is not None:
                self.drain.close()

    def uncertain(self):
        """Creation may have scheduled work without returning a completion receipt."""
        from hermes_cli.worker_drain_capability import revoke

        try:
            if self.drain is not None:
                revoke('local-work-task-outcome-uncertain')
        finally:
            self.finish()
