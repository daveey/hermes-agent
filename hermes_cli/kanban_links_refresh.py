"""Bounded, context-captured background checks of last-good link projections."""
from contextvars import Context, copy_context
import logging
import threading
import time
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
    queued scope coalesce. Starts of each scope are at least three seconds apart;
    a trailing queued check wakes at its deadline without further display polls.
    Both the queue and cooldown history are capacity-bounded. If cooldown history
    is full, new scopes wait for its earliest expiry rather than evicting a live
    cooldown (which would let scope churn bypass the floor). A full queue leaves
    last-good data intact; a later poll retries admission. Cold and strict reads
    do not use this worker.
    """

    def __init__(self, refresh: Callable[[tuple], None], capacity: int, *,
                 clock: Callable[[], float] = time.monotonic):
        self._refresh = refresh
        self._capacity = capacity
        self._clock = clock
        self._next_start: dict[tuple, float] = {}
        self._ready = threading.Condition()
        self._pending: dict[tuple, Context] = {}
        self._worker: threading.Thread | None = None
        self._fenced: set[tuple] = set()
        self._active: tuple | None = None

    def drain(self, scope: tuple, *, timeout: float) -> None:
        """Fence admission, discard queued work, and join an active scope.

        A timeout retains the fence: the caller still owns the transition and
        must retry drain or explicitly resume. Never call under a projection lock.
        """
        with self._ready:
            self._fenced.add(scope)
            if self._active != scope:
                self._pending.pop(scope, None)
                self._ready.notify_all()
                return
            if not self._ready.wait_for(lambda: self._active != scope, timeout):
                raise TimeoutError('remote links scope is still refreshing')

    def resume(self, scope: tuple) -> None:
        """Restore legacy admission after replacement-source cleanup."""
        with self._ready:
            self._fenced.discard(scope)

    @property
    def busy(self) -> bool:
        with self._ready:
            return bool(self._pending)

    def submit(self, scope: tuple) -> None:
        with self._ready:
            if scope in self._fenced or scope in self._pending or len(self._pending) >= self._capacity:
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
                while True:
                    now = self._clock()
                    self._next_start = {key: due for key, due in self._next_start.items() if due > now}
                    scope = next((key for key in self._pending
                                  if key not in self._next_start and len(self._next_start) < self._capacity), None)
                    if scope is not None:
                        break
                    # One deadline wait, not periodic polling. Empty workers retain
                    # no context and need no wake until a new submission arrives.
                    delay = min(self._next_start.values()) - now if self._pending else None
                    self._ready.wait(delay)
                self._next_start[scope] = now + 3.0
                self._active = scope
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
                    context = None  # release captured values before acknowledging drain
                    self._active = None
                    self._ready.notify_all()
