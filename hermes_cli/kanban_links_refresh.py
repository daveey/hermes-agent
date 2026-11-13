"""Bounded, context-captured background checks of last-good link projections."""
from contextvars import Context, copy_context
import logging
import threading
from collections.abc import Callable

_log = logging.getLogger(__name__)


class ScopeRefresher:
    """One lazy process-lifetime daemon; pending and active scopes share one bound.

    No projection IO runs under the condition. Each job owns its captured context,
    which is released before the worker goes idle. Repeated polls of an active or
    queued scope coalesce. A full queue leaves last-good data intact; a later poll
    retries admission. Cold and strict reads do not use this worker.
    """

    def __init__(self, refresh: Callable[[tuple], None], capacity: int):
        self._refresh = refresh
        self._capacity = capacity
        self._ready = threading.Condition()
        self._pending: dict[tuple, Context] = {}
        self._worker: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._ready:
            return bool(self._pending)

    def submit(self, scope: tuple) -> None:
        with self._ready:
            if scope in self._pending or len(self._pending) >= self._capacity:
                return
            self._pending[scope] = copy_context()
            self._ready.notify()
            if self._worker is not None:
                return
            try:
                # Explicit empty outer context: only each queued job carries scope.
                worker = threading.Thread(target=Context().run, args=(self._run,),
                                          daemon=True, name='kanban-remote-links')
                worker.start()
                self._worker = worker
            except RuntimeError:
                self._pending.clear()
                _log.warning('kanban remote links: could not start background refresh', exc_info=True)

    def _run(self) -> None:
        while True:
            with self._ready:
                while not self._pending:
                    self._ready.wait()
                scope = next(iter(self._pending))
                context = self._pending[scope]
            try:
                from hermes_cli import loop_accounting  # one ``links_refresh`` pass per rebuild
                context.run(loop_accounting.timed, "links_refresh", self._refresh, scope)
            except Exception:  # health: allow BLE001 -- refresh failure retains last-good data; later polls retry
                _log.warning('kanban remote links: background rebuild failed; serving the previous projection',
                             exc_info=True)
            finally:
                with self._ready:
                    del self._pending[scope]
                context = None  # release captured request/profile values before going idle
