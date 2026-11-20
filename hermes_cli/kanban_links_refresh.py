"""Bounded, context-captured background checks of last-good link projections."""
from contextvars import Context, copy_context
import logging
import threading
from collections.abc import Callable

_log = logging.getLogger(__name__)


def record_decision(hit: tuple | None, fingerprint: tuple, *, fresh: bool, now: float) -> None:
    """Bounded counters for background checks; raw store changes may overlap expiry.

    No IO or input probing here. Store categories describe changed fingerprint
    entries, not necessarily semantically relevant changes (narrowing can ignore
    an unread board). Only non-fresh decisions report changed-store counters.
    """
    from hermes_cli import loop_accounting as la
    if fresh:
        reason = "skip"
    elif hit is None:
        reason = "cold"
    else:
        age, changed = now >= hit[0], hit[2] != fingerprint
        reason = "age_inputs" if age and changed else "age" if age else "inputs"
        if changed:
            kinds = set()
            for entry in (*hit[2], *fingerprint):
                if entry in hit[2] and entry in fingerprint:
                    continue
                if isinstance(entry, tuple) and entry and isinstance(entry[0], str):
                    name = entry[0].rsplit("/", 1)[-1]
                    kind = "ledger" if name in ("origin-ledger.db", "origin-ledger.db-wal") else (
                        "board" if len(entry) == 2 else "hub")
                else:
                    kind = "unknown"
                kinds.add(kind)
            for kind in kinds:
                with la.pass_("links_refresh.changed." + kind):
                    pass
    with la.pass_("links_refresh." + reason) as accounting:
        if fresh:
            accounting.skip()


def refresh_decided(hit: tuple | None, fingerprint: tuple, fresh: bool, now: float, build: Callable[[], object]) -> None:
    """Count one background check's decision, and run ``build`` (accounted as a build) unless it is fresh."""
    from hermes_cli import loop_accounting as la
    record_decision(hit, fingerprint, fresh=fresh, now=now)
    if not fresh:
        with la.pass_("links_refresh.build"):
            build()


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
                from hermes_cli import loop_accounting  # one check; nested .build counts actual rebuilds
                context.run(loop_accounting.timed, "links_refresh", self._refresh, scope)
            except Exception:  # health: allow BLE001 -- refresh failure retains last-good data; later polls retry
                _log.warning('kanban remote links: background rebuild failed; serving the previous projection',
                             exc_info=True)
            finally:
                with self._ready:
                    del self._pending[scope]
                context = None  # release captured request/profile values before going idle
