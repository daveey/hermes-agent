"""Code-skew retirement for Desktop-owned ``hermes serve --isolated`` backends reached over SSH.

Such a backend belongs to a Desktop on another machine: the host's updater never restarts it (only
the remote client holds its token and owner nonce), so after ``hermes update`` it keeps serving the
code it imported at startup against a tree that has moved on, and lazily imports new modules into
an old process. The idle watchdog (``web_server_idle_exit``) only fires once every client has left,
which a connected Desktop never does.

This watchdog polls ``gateway.code_skew`` (the boot revision the serve lifespan records, compared
with the checkout on disk) and, once no update is in flight and the retirement fence proves the
backend idle (and closes admission so no turn can start mid-teardown), exits cleanly. The client's
reconnect path respawns it from the new code. Every uncertain read (unknown revision, busy or
indeterminate ledgers, a live update) keeps the process up.

The checkout revision is not the only thing an update replaces: a runtime rebuild can swap the
venv/site-packages under the backend while the revision stays put. ``/api/ssh/ownership`` then
reports ``runtimeIntact: false`` and every client refuses to reuse the backend, so the watchdog
also retires on a lost runtime marker; otherwise a long-lived backend (the always-on dashboard)
would sit stale and unreusable until the next checkout change.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

_log = logging.getLogger(__name__)

DEFAULT_SKEW_POLL_S = 30.0
# [daveey] The superseded watchdog reads one small file per poll, so it polls fast: an idle superseded
# always-on backend retires within two polls of the switch and its paused turns move to the successor.
SUPERSEDED_POLL_S = 2.0
# ... but a superseded backend that is busy (a live turn refused the fence) re-tries the fence only this often.
SUPERSEDED_BUSY_POLL_S = 10.0
# Two consecutive observations: a checkout mid-rewrite (ref file replaced, packed-refs repacked) can
# briefly read differently; a real update reads the new revision on every poll after it finishes.
_CONFIRMATIONS = 2

# ``(boot_rev, disk_rev)`` when the checkout drifted since boot, else None (``detect_code_skew``).
Skew = Optional[tuple[str, str]]

# [daveey] Retirement keys of ``host_loop_lease.retire_host_loops`` (t_01a1d65520e78e7d). A backend this module
# will retire (superseded, or its boot commit behind the checkout) gives its host-loop leases up at once: an
# old always-on backend took ``kanban-unloaded`` while the gateway restarted on a fix, then ran a sweep wake on
# the pre-fix code during its drain, and that wake's quota-wall failure deferred the chat's notice by 4 days.
_SUPERSEDED_KEY = "always-on-superseded"
_SKEW_KEY = "code-skew"
# A superseded watchdog polls every 2 s; its skew read (git ref files) runs at most this often.
STALE_CHECK_S = 10.0


def _retire_host_loops(key: str, reason: str) -> None:
    try:
        from hermes_cli.host_loop_lease import retire_host_loops

        retire_host_loops(key, reason)
    except Exception:  # health: allow BLE001 -- best effort: a broken lease module must not stop the watchdog
        _log.warning("could not retire this process's host loops (%s)", reason, exc_info=True)


def _resume_host_loops(key: str) -> None:
    try:
        from hermes_cli.host_loop_lease import resume_host_loops

        resume_host_loops(key)
    except Exception:  # health: allow BLE001 -- see _retire_host_loops
        _log.warning("could not resume this process's host loops (%s)", key, exc_info=True)


class StaleCodeLoops:
    """Host loops of a process whose boot commit is behind the checkout: retired after ``_CONFIRMATIONS``
    consecutive skew reads, resumed when the checkout reads as the boot commit again. Reads at most every
    ``every_s`` (``clock`` time); an unreadable revision reads as no skew (never a false retirement)."""

    def __init__(self, skew_fn: Callable[[], Skew], *, every_s: float = 0.0,
                 clock: Callable[[], float] = time.monotonic):
        self._skew_fn, self._every_s, self._clock = skew_fn, every_s, clock
        self._seen = 0
        self._next = 0.0
        self.retired = False

    def observe(self, skew: Skew = None, *, read: bool = True) -> None:
        if read:
            now = self._clock()
            if now < self._next:
                return
            self._next = now + self._every_s
            try:
                skew = self._skew_fn()
            except Exception:  # health: allow BLE001 -- an unreadable revision is no proof of skew
                skew = None
        self._seen = self._seen + 1 if skew else 0
        if skew and self._seen >= _CONFIRMATIONS and not self.retired:
            self.retired = True
            _retire_host_loops(_SKEW_KEY, "this process loaded {} but the checkout is at {}".format(*skew))
        elif not skew and self.retired:
            self.retired = False
            _resume_host_loops(_SKEW_KEY)


def should_retire_for_skew(*, skew: Skew, update_in_progress: bool) -> bool:
    """Retire only on a proven code change, and never while the tree may still be mid-swap."""
    return bool(skew) and not update_in_progress


def _update_in_progress() -> bool:
    try:
        from hermes_cli.update_lock import update_in_progress

        return update_in_progress()
    except Exception:
        return True  # cannot prove the swap is over: wait


def _runtime_intact() -> bool:
    try:
        from hermes_cli.web_server import _ssh_runtime_intact

        return _ssh_runtime_intact()
    except Exception:
        return True  # cannot prove the runtime moved: stay up


def _runtime_reattach() -> bool:
    try:
        from hermes_cli.web_server import _ssh_runtime_reattach

        return _ssh_runtime_reattach()
    except Exception:
        return False  # cannot prove the bytes are unchanged: treat the loss as a real swap


# [daveey] A runtime retirement respawns the backend on the same nonce (the always-on dashboard
# keeps it fixed), so a cause that keeps deleting the marker turns retirement into a restart loop
# that no in-process state can see. The last runtime retirement per nonce is persisted, and a second
# one inside this window is held: the backend keeps serving (Desktop already refuses to reuse it on
# ``runtimeIntact: false``) instead of exiting every minute. Code-revision retirement is not held.
RUNTIME_RETIRE_BACKOFF_S = 900.0


class RuntimeRetireLedger:
    """Wall-clock time of the last runtime retirement per owner nonce, shared across restarts."""

    def __init__(self, path=None):
        if path is None:
            from hermes_constants import get_hermes_home

            path = get_hermes_home() / "cache" / "ssh-runtime-retirements.json"
        self.path = path

    def _read(self) -> dict:
        import json

        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def last(self, key: str) -> Optional[float]:
        value = self._read().get(key)
        return float(value) if isinstance(value, (int, float)) else None

    def record(self, key: str, when: float) -> None:
        import json
        import os

        data = {k: v for k, v in self._read().items() if isinstance(v, (int, float)) and when - v < 7 * 86400}
        data[key] = when
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            _log.warning("could not record the runtime retirement in %s: %s", self.path, exc)


def _runtime_key() -> str:
    try:
        from hermes_cli import web_server

        return web_server._SSH_OWNER_NONCE or web_server._SSH_RUNTIME_MARKER or "default"
    except Exception:
        return "default"


def _run_retirement_watchdog(server, *, observe: Callable[[], Optional[str]], fence, poll_s: float,
                             max_polls: Optional[int], thread_name: str,
                             on_retire: Optional[Callable[[str], None]] = None,
                             busy_poll_s: Optional[float] = None) -> threading.Thread:
    """Daemon thread shared by the SSH-isolated retirement watchdogs: ``observe`` returns the log
    reason while retirement is warranted (else None); after ``_CONFIRMATIONS`` consecutive reasons
    the backend exits only through the retirement fence (proven idle, admission closed first). After a
    refused prepare the next poll waits ``busy_poll_s`` (default ``poll_s``)."""
    if fence is None:
        from hermes_cli.backend_retirement import retirement

        fence = retirement

    def _loop() -> None:
        seen = polls = 0
        while not getattr(server, "should_exit", False) and (max_polls is None or polls < max_polls):
            polls += 1
            reason = observe()
            seen = seen + 1 if reason else 0
            wait = poll_s
            if reason and seen >= _CONFIRMATIONS:
                permit = fence.prepare()
                if permit.get("ok") and fence.commit(permit.get("token")).get("ok"):
                    _log.warning("%s", reason)
                    if on_retire is not None:
                        on_retire(reason)
                    server.should_exit = True
                    return
                wait = poll_s if busy_poll_s is None else busy_poll_s
            time.sleep(wait)

    thread = threading.Thread(target=_loop, daemon=True, name=thread_name)
    thread.start()
    return thread


# [daveey] Live code-skew watchdog threads of this process. A core-update-paused turn asks whether
# anything will retire this backend before it waits for a successor (tui_gateway.update_pause).
_SKEW_WATCHDOGS: list[threading.Thread] = []


def skew_watchdog_armed() -> bool:
    """Whether this process runs a code-skew retirement watchdog that is still polling."""
    return any(thread.is_alive() for thread in _SKEW_WATCHDOGS)


_RUNTIME_REASON = ("SSH-isolated backend's runtime was replaced under it (runtimeIntact false); "
                   "retiring so its client respawns it on the new runtime.")


def start_code_skew_watchdog(server, *, skew_fn: Optional[Callable[[], Skew]] = None,
                             update_probe: Callable[[], bool] = _update_in_progress,
                             runtime_probe: Callable[[], bool] = _runtime_intact,
                             runtime_reattach: Callable[[], bool] = _runtime_reattach,
                             ledger: Optional[RuntimeRetireLedger] = None,
                             runtime_key: Callable[[], str] = _runtime_key,
                             backoff_s: float = RUNTIME_RETIRE_BACKOFF_S,
                             clock: Callable[[], float] = time.time,
                             fence=None, poll_s: float = DEFAULT_SKEW_POLL_S,
                             max_polls: Optional[int] = None) -> threading.Thread:
    """Daemon thread that sets ``server.should_exit`` once the loaded code is provably stale and
    the backend is provably idle. ``max_polls`` bounds the loop for tests only."""
    if skew_fn is None:
        from gateway.code_skew import detect_code_skew

        skew_fn = detect_code_skew
    read_skew: Callable[[], Skew] = skew_fn
    stale = StaleCodeLoops(read_skew)
    held_until: list[float] = [0.0]

    def _ledger() -> Optional[RuntimeRetireLedger]:
        nonlocal ledger
        if ledger is None:
            try:
                ledger = RuntimeRetireLedger()
            except Exception:
                return None
        return ledger

    def _observe() -> Optional[str]:
        skew = read_skew()
        stale.observe(skew, read=False)  # a stale process runs no host pass while it waits for its fence
        if skew and should_retire_for_skew(skew=skew, update_in_progress=update_probe()):
            return ("SSH-isolated backend loaded {} but the install is now at {}; "
                    "retiring so its client respawns it on the new code.".format(*skew))
        if runtime_probe() or update_probe():
            return None
        if runtime_reattach():
            # Same bytes re-realized (a PM repair): the marker is back and the backend is reusable.
            _log.info("SSH-isolated backend's runtime was re-realized with identical content; "
                      "runtime marker restored, not retiring.")
            return None
        book = _ledger()
        last = book.last(runtime_key()) if book is not None else None
        now = clock()
        if last is not None and 0 <= now - last < backoff_s:
            if held_until[0] != last + backoff_s:
                held_until[0] = last + backoff_s
                _log.warning("SSH-isolated backend's runtime marker is gone again %.0fs after the last "
                             "runtime retirement; holding retirement for %.0fs so one bad signal "
                             "cannot restart it every minute.", now - last, backoff_s - (now - last))
            return None
        return _RUNTIME_REASON

    def _on_retire(reason: str) -> None:
        if reason is _RUNTIME_REASON:
            book = _ledger()
            if book is not None:
                book.record(runtime_key(), clock())

    thread = _run_retirement_watchdog(server, observe=_observe, fence=fence, poll_s=poll_s,
                                      max_polls=max_polls, thread_name="ssh-isolated-skew-watchdog",
                                      on_retire=_on_retire)
    _SKEW_WATCHDOGS[:] = [t for t in _SKEW_WATCHDOGS if t.is_alive()] + [thread]
    return thread


# [daveey] Keep-alive drain of a superseded always-on backend. Its clients' keep-alive sockets stay pinned
# to it through the TCP-splice proxy, and retiring with them open hangs up every in-flight request
# (Desktop then tears down and redials its SSH transport). While draining, every HTTP response carries
# ``Connection: close`` so each pinned socket closes cleanly after its next response and the client's next
# request opens a new proxy connection to the active backend. The watchdog asks the fence only once the
# backend was superseded for DRAIN_MIN_S (> uvicorn's 5 s keep-alive timeout + one poll, so idle pinned
# sockets are gone) and has no open HTTP connection, or after DRAIN_CAP_S (a long stream, unreadable state).
DRAIN_MIN_S = 8.0
DRAIN_CAP_S = 30.0
_DRAINING = threading.Event()
_CLOSE = (b"connection", b"close")


def draining() -> bool:
    return _DRAINING.is_set()


def _close_headers(headers) -> list:
    """Response headers with ``connection: close`` (replacing a keep-alive value; an upgrade is kept)."""
    out, seen = [], False
    for name, value in headers or []:
        if bytes(name).lower() == b"connection":
            if b"upgrade" in bytes(value).lower():
                return list(headers)
            if not seen:
                out.append(_CLOSE)
                seen = True
            continue
        out.append((name, value))
    if not seen:
        out.append(_CLOSE)
    return out


def wrap_asgi_close_when_draining(app, *, is_draining: Callable[[], bool] = draining):
    """ASGI wrapper: while this backend drains, HTTP responses ask the client to close the connection.
    WebSocket and lifespan scopes pass straight through; a no-op unless the superseded watchdog drains."""

    async def _app(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)

        async def _send(message):
            if message.get("type") == "http.response.start" and is_draining():
                message = {**message, "headers": _close_headers(message.get("headers"))}
            await send(message)

        return await app(scope, receive, _send)

    return _app


def open_http_connections(server) -> Optional[int]:
    """Open uvicorn HTTP connections (keep-alive or in flight; WebSockets excluded), or None when unreadable.
    The set is mutated on the event loop thread, so it is copied and a concurrent change reads as unknown."""
    try:
        conns = list(server.server_state.connections)
    except (AttributeError, RuntimeError, TypeError):
        return None
    return sum(1 for c in conns if type(c).__module__.startswith("uvicorn.protocols.http"))


def start_superseded_watchdog(server, *, port: int, superseded: Optional[Callable[[int], bool]] = None,
                              pending: Optional[Callable[[int], bool]] = None,
                              fence=None, poll_s: float = SUPERSEDED_POLL_S,
                              max_polls: Optional[int] = None,
                              http_connections: Optional[Callable[[], Optional[int]]] = None,
                              drain_min_s: float = DRAIN_MIN_S, drain_cap_s: float = DRAIN_CAP_S,
                              clock: Callable[[], float] = time.monotonic,
                              skew_fn: Optional[Callable[[], Skew]] = None,
                              stale_check_s: float = STALE_CHECK_S) -> threading.Thread:
    """[daveey] An always-on blue/green backend (``hermes_cli/always_on_rollover``) runs this INSTEAD of the
    code-skew watchdog: the roller replaces it with a warmed successor, so it must not exit on skew before
    that successor serves. It retires (through the same idle fence) once ``active.json`` names neither its
    port as active nor as warming. Registered as a skew watchdog: a core-update-paused turn still learns
    that this process will be replaced (``tui_gateway.update_pause``), and ``successor_pending`` tells it the
    successor is still booting or warming, which takes longer than a plain skew retirement. The poll is
    fast (a file read), so an idle superseded backend hands its paused turns over seconds after the switch."""
    from hermes_cli import always_on_rollover

    check: Callable[[int], bool] = superseded or always_on_rollover.is_superseded
    on_the_way: Callable[[int], bool] = pending or always_on_rollover.successor_pending
    count: Callable[[], Optional[int]] = http_connections or (lambda: open_http_connections(server))
    if skew_fn is None:
        from gateway.code_skew import detect_code_skew

        skew_fn = detect_code_skew
    # The roller replaces a stale always-on backend only once a successor is warm; until then its old code
    # must not hold host-loop leases (it took kanban-unloaded on 17f28630e4 while the gateway restarted).
    stale = StaleCodeLoops(skew_fn, every_s=stale_check_s)  # own monotonic clock: ``clock`` times the drain
    since: list[Optional[float]] = [None]

    def _observe() -> Optional[str]:
        stale.observe()
        if not check(port):
            if since[0] is not None:
                _log.info("always-on backend :%s is active again; keep-alive drain stopped", port)
                _resume_host_loops(_SUPERSEDED_KEY)
            since[0] = None
            _DRAINING.clear()
            return None
        now = clock()
        if since[0] is None:
            since[0] = now
            _DRAINING.set()
            _log.info("always-on backend :%s superseded; draining keep-alive connections", port)
            # Only the connections already open drain here: host passes and their sweep wakes go to the
            # successor (or the gateway) now, never to this process's stale code (t_01a1d65520e78e7d).
            _retire_host_loops(_SUPERSEDED_KEY, f"always-on backend :{port} superseded")
        elapsed = now - since[0]
        if elapsed < drain_cap_s:
            if elapsed < drain_min_s:
                return None
            open_now = count()
            if open_now is None or open_now > 0:
                return None
        return (f"always-on backend :{port} was superseded by a warmed successor (active.json); "
                f"retiring now that it is idle (drained {elapsed:.0f}s).")

    thread = _run_retirement_watchdog(server, observe=_observe, fence=fence, poll_s=poll_s,
                                      max_polls=max_polls, thread_name="always-on-superseded-watchdog",
                                      busy_poll_s=max(poll_s, SUPERSEDED_BUSY_POLL_S))
    _SKEW_WATCHDOGS[:] = [t for t in _SKEW_WATCHDOGS if t.is_alive()] + [thread]
    _SUCCESSOR_PROBES[:] = [(t, p) for t, p in _SUCCESSOR_PROBES if t.is_alive()]
    _SUCCESSOR_PROBES.append((thread, lambda: on_the_way(port)))
    return thread


# [daveey] Live superseded watchdogs with their "successor on the way" probe (``successor_pending``).
_SUCCESSOR_PROBES: list[tuple[threading.Thread, Callable[[], bool]]] = []


def successor_pending() -> bool:
    """Whether a live superseded watchdog in this process says its warmed successor is being prepared (or
    already serves): a paused turn then waits past ``update_pause.HANDOFF_WAIT_S`` for it. Errors read False."""
    for thread, probe in _SUCCESSOR_PROBES.copy():
        if thread.is_alive():
            try:
                if probe():
                    return True
            except (OSError, ValueError, TypeError, KeyError):
                _log.debug("successor probe failed", exc_info=True)
    return False
