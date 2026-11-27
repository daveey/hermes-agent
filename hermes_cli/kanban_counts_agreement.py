"""Bounded sidebar-count agreement for a session outcome's empty card list. [daveey]

``hermes_state_outcome._read_card_items`` refuses an empty card list unless the sidebar counts agree that the
chat has no open work. Those counts are a single-flight recompute (``_counts_flight``) that can itself wait on
the remote projection (``_links_lock``), for many seconds on a busy hub. The user-steps controls refresh runs on
one worker thread per process, so it parked there and a withdrawn ask kept its red ``human_ask`` envelope until
some unrelated refresh ran (t_01a21c00a2e628c8).

Here the recompute runs on one background thread per origin scope. A caller waits for it at most ``WAIT_S``. On
timeout it raises ``CountsPending``: the card source is then unknown coverage (never done, never a human ask) and
the caller's ``on_published`` callback runs once that recompute settles, so the chat refreshes again with no
other trigger. A stale snapshot is never used: zero open work from an outdated snapshot is not agreement.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable, Hashable

logger = logging.getLogger(__name__)

WAIT_S = 1.0
FOREGROUND_WAIT_S = 30.0
_foreground_deadline: ContextVar[float | None] = ContextVar("count_agreement_deadline", default=None)


@contextmanager
def foreground_counts():
    """Finite readers cannot rely on a passive callback after returning/printing.

    Share one 30s count-wait budget across a finite reader's rows and fence retries,
    returning unknown on exhaustion/errors. Live controls keep their 1s/deferred retry.
    Nested refreshes retain their reader's deadline; interactive workers are unchanged.
    """
    if _foreground_deadline.get() is not None:
        yield
        return
    token = _foreground_deadline.set(time.monotonic() + FOREGROUND_WAIT_S)
    try:
        yield
    finally:
        _foreground_deadline.reset(token)



class _Flight:
    __slots__ = ("callbacks", "done", "error", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: tuple[dict[str, int], dict[str, int]] | None = None
        self.error: BaseException | None = None
        self.callbacks: dict[Hashable, Callable[[], object]] = {}


_lock = threading.Lock()
_flights: dict[tuple, _Flight] = {}


class CountsPending(RuntimeError):
    """The published counts are not fresh for the current fingerprint and the caller may not wait."""


def fresh_open_counts() -> tuple[dict[str, int], dict[str, int]]:
    """``(active, blocked)`` exactly as ``_all_counts`` would return them without entering ``_counts_flight``:
    the published snapshot when it is fresh for this scope and fingerprint, else ``CountsPending``. Never a stale
    snapshot: zero open work from an outdated one is not agreement."""
    import time

    from hermes_cli import kanban_session_activity as ksa

    scope = ksa._origin_scope()
    fingerprint = ksa._activity_fingerprint()
    with ksa._cache_lock:
        ksa._count_scopes.restore_locked(scope)
        if ksa._revalidate.counts_fresh(scope, fingerprint, time.monotonic()):
            result = ksa._cached_count_result(False, False)
            return result[0], result[1]
    raise CountsPending("kanban counts are recomputing")


def _run(scope: tuple, flight: _Flight) -> None:
    from hermes_cli import kanban_session_activity as ksa

    try:
        # The public readers, so the blocking callers' semantics (and their test doubles) are unchanged.
        flight.result = (ksa.active_task_counts(), ksa.blocked_task_counts())
    except BaseException as exc:  # health: allow BLE001 -- replayed to the waiters, never swallowed
        flight.error = exc
    finally:
        with _lock:
            flight.done.set()
            if _flights.get(scope) is flight:
                del _flights[scope]
            callbacks = list(flight.callbacks.values())
            flight.callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:  # health: allow BLE001 -- one chat's retry never stops the others
                logger.warning("kanban count agreement: deferred refresh failed", exc_info=True)


def agreed_counts(wait_s: float = WAIT_S, *, key: Hashable | None = None,
                  on_published: Callable[[], object] | None = None) -> tuple[dict[str, int], dict[str, int]]:
    """``(active, blocked)`` from a fresh snapshot, or from a recompute that settled within ``wait_s``.

    Otherwise ``CountsPending``; ``on_published`` (deduplicated by ``key``) then runs once that recompute settles,
    on its thread. A recompute failure is re-raised to every waiter that saw it settle."""
    from agent.memory_provider import spawn_context_thread
    from hermes_cli import kanban_session_activity as ksa

    try:
        return fresh_open_counts()
    except CountsPending:
        pass
    scope = ksa._origin_scope()
    start = False
    with _lock:
        flight = _flights.get(scope)
        if flight is None:
            flight = _flights[scope] = _Flight()
            start = True
    if start:  # outside ``_lock``: the recompute's settle path takes it
        # Shared by every waiter of this scope: never custody of the first caller's local-work scope.
        try:
            # A zero-argument target: inline ``Thread`` doubles in gateway suites call ``target()`` and drop ``args``.
            spawn_context_thread(functools.partial(_run, scope, flight), name="kanban-counts-agreement",
                                 passive=True).start()
        except BaseException as exc:  # health: allow BLE001 -- settle the flight, never leave it pending forever
            with _lock:
                flight.error = exc
                flight.done.set()
                if _flights.get(scope) is flight:
                    del _flights[scope]
            raise
    deadline = _foreground_deadline.get()
    if deadline is not None:
        wait_s = max(0.0, deadline - time.monotonic())
        on_published = None  # no late projection write racing the finite reader's result
    flight.done.wait(max(0.0, wait_s))
    with _lock:
        if not flight.done.is_set():
            if on_published is not None:
                flight.callbacks.setdefault(key if key is not None else on_published, on_published)
            raise CountsPending("kanban counts are recomputing")
    if flight.error is not None:
        raise flight.error
    if flight.result is None:  # unreachable: a settled flight carries a result or an error
        raise CountsPending("kanban count recompute settled without a result")
    return flight.result


def _reset_for_tests() -> None:
    with _lock:
        _flights.clear()
