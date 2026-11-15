"""[daveey] FD-safe release of a per-call aux client that an abandoned provider daemon may still be reading.

``auxiliary_client._run_protected_sync_provider_call`` runs the provider callback on a
``hermes-protected-aux-provider`` daemon so the owner can unwind at once on a hard cancel (a user Stop,
the core-update pause's compression yield). The daemon keeps reading the client's TLS stream after that.
Class routing builds one client per call and closed it in the owner's ``finally``: ``httpx`` then
``close()``d the socket the daemon was blocked on. The SSL object keeps the raw fd number in its BIO, the
kernel hands that number to the next ``open()`` (a SQLite database), and the daemon's next ``SSL_read``
takes ``SQLit`` from the file header, finds no valid record and writes a 24-byte TLS 1.3 alert at offset 5
(hub ``inbox/inbox.db`` 2026-11-13 09:01:27, 3 ms after a compression yield; ``user-steps/actions.sqlite3``
2026-11-11).

The owner now only shuts the sockets down (``force_close_tcp_sockets``: FD-safe from any thread, wakes the
daemon's read at once and stops billing an abandoned summary) and hands the real close to the daemon,
which runs it right after its callback returns, on the thread that owns the socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import threading
from typing import Any, Callable, Iterator, TypeVar

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

_ABANDONED: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "hermes_aux_abandoned_provider_calls", default=None)


class ProviderAttempt:
    """One protected provider call's daemon. Callbacks deferred to it run on the daemon once its provider
    callback returned (or at once from ``defer``'s caller when it already has)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._finished = False
        self._on_finish: list[Callable[[], None]] = []
        self._released = threading.Event()

    def finish(self) -> None:
        """Daemon side, after the provider callback returned or raised: run the deferred releases here."""
        with self._lock:
            self._finished = True
            pending, self._on_finish = self._on_finish, []
        try:
            for release in pending:
                try:
                    release()
                except Exception:
                    logger.debug("deferred aux client release failed", exc_info=True)
        finally:
            self._released.set()

    def wait(self, timeout: float) -> bool:
        """Owner side: block until the daemon returned and ran its deferred releases (False on timeout)."""
        return self._released.wait(timeout)

    def defer(self, release: Callable[[], None]) -> bool:
        """Queue ``release`` for the daemon; False when the daemon already finished (the caller runs it)."""
        with self._lock:
            if self._finished:
                return False
            self._on_finish.append(release)
            return True


def note_abandoned(attempt: ProviderAttempt) -> None:
    """Owner side, on hard cancel: the daemon still runs; record it in the enclosing ``client_scope``."""
    abandoned = _ABANDONED.get()
    if abandoned is not None:
        abandoned.append(attempt)


async def run_in_thread(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """``asyncio.to_thread`` for a provider call that reads a client's socket.

    A cancelled await does not stop the worker: it keeps reading while the awaiting task unwinds through its
    ``client_scope`` on the loop thread. The worker is then recorded as abandoned there, so the scope only
    shuts the sockets down and the worker closes the client once ``fn`` returns. A worker cancelled before
    it started never touches the socket and is not recorded.

    ``force_close_tcp_sockets`` walks httpx pools only, so it cannot reach Bedrock's boto3/urllib3 sockets: a
    cancelled Bedrock worker logs ``tcp_shutdown=0``. Its fd stays safe (``BedrockAuxiliaryClient.close`` is a
    no-op, so no thread closes those sockets), but the provider read keeps running, and billing, until its own
    timeout. The sync twin (``_run_protected_sync_provider_call``) behaves the same way.
    """
    attempt, lock, state = ProviderAttempt(), threading.Lock(), ["pending"]

    def worker() -> _T:
        with lock:
            if state[0] == "dropped":
                return None  # type: ignore[return-value]  # nobody awaits it
            state[0] = "running"
        try:
            return fn(*args, **kwargs)
        finally:
            attempt.finish()

    try:
        return await asyncio.to_thread(worker)
    except asyncio.CancelledError:
        with lock:
            started = state[0] == "running"
            if not started:
                state[0] = "dropped"
        if started:
            note_abandoned(attempt)
        raise


@contextlib.contextmanager
def collect_abandoned(abandoned: list[ProviderAttempt]) -> Iterator[list[ProviderAttempt]]:
    """Record into ``abandoned`` each ``run_in_thread`` call cancelled mid-run inside this block. In an async
    generator the block must not span a ``yield``: it has to open and close within one step of the task."""
    token = _ABANDONED.set(abandoned)
    try:
        yield abandoned
    finally:
        _ABANDONED.reset(token)


@contextlib.contextmanager
def client_scope(client: Any, close: Callable[[Any], None]) -> Iterator[None]:
    """Own ``client`` for one provider call and ``close`` it on exit, on a thread allowed to.

    No abandoned daemon: closed here, as before. Otherwise the sockets are shut down from here and the
    close runs on the last abandoned daemon when its callback returns."""
    abandoned: list[ProviderAttempt] = []
    try:
        with collect_abandoned(abandoned):
            yield
    finally:
        release_client(client, close, abandoned)


def release_client(client: Any, close: Callable[[Any], None], abandoned: list[ProviderAttempt],
                   *, shutdown: Callable[[Any], int] | None = None) -> None:
    """Close ``client`` here, or (abandoned daemons) shut its sockets down and defer the close to them.
    ``shutdown`` (default: the client's pool sweep) returns how many sockets it shut down."""
    if not abandoned:
        close(client)
        return
    if shutdown is None:
        from agent.agent_runtime_helpers import force_close_tcp_sockets
        shutdown = lambda owned: force_close_tcp_sockets(getattr(owned, "_real_client", None) or owned)
    remaining = [len(abandoned)]
    lock = threading.Lock()
    swept = threading.Event()

    def one_done() -> None:
        with lock:
            remaining[0] -= 1
            last = remaining[0] == 0
        if last:
            close(client)

    def daemon_done() -> None:
        # A daemon that finishes between the defer and the sweep below must not close while this thread still
        # shuts the same sockets down: shutdown() reads the fd outside the GIL and could hit a recycled number.
        swept.wait()
        one_done()

    # Defer before the shutdown wakes the daemon: shutting down first let a woken daemon finish before the
    # defer, so the close fell back to this thread (FD-safe then, but no longer the daemon's).
    # The defers run inside the try: if one raises after another succeeded (or an async KeyboardInterrupt
    # lands here), the finally still opens the gate and counts down every attempt that took no share, so the
    # daemons already holding one close the client instead of parking forever in swept.wait().
    deferred = 0
    try:
        for attempt in abandoned:
            deferred += bool(attempt.defer(daemon_done))
        shutdown_count = shutdown(client) if deferred else 0
    finally:
        swept.set()
        for _ in range(len(abandoned) - deferred):
            one_done()
    if deferred:
        logger.info("auxiliary client close handed to %d abandoned provider daemon(s) (tcp_shutdown=%d)",
                    deferred, shutdown_count)


def provider_worker_scope() -> Any:
    """Loop-accounting residual scope ``aux_provider/<initiating operation>`` for a protected provider daemon
    (run inside the daemon, whose context copy carries the relay task name; never prompt or token content).
    A no-op context when accounting cannot load."""
    try:
        from agent.auxiliary_client import _RELAY_AUX_CALL_CONTEXT
        from hermes_cli import loop_accounting
        operation = str((_RELAY_AUX_CALL_CONTEXT.get() or {}).get("task") or "unknown")[:48]
        return loop_accounting.scope(f"aux_provider/{operation}")
    except Exception:  # health: allow BLE001 -- accounting must never fail a provider call
        logger.debug("aux provider scope unavailable", exc_info=True)
        return contextlib.nullcontext()
