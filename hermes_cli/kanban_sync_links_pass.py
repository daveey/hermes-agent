"""The sync loop's chat-link pass, on its own thread so it can never hold up a sync round. [daveey]

Inline in ``kanban_sync_client._run_forever``, a slow pass held the replica pull, the remote-parent mirror and the
outbox push for its whole length: on the hub 2026-11-13 one pass took ~10 min (04:14-04:24Z, its strict ledger read
queued on ``_links_lock`` behind Desktop readers) and the replica went 13 min stale, so hub cards waiting on worker
cards did not promote (t_01a1d2cc49634a78). The loop asks for a pass on its cadence; a request while one is running
is refused and asked again on a later iteration, so passes never pile up. The thread owns its own connection scope
and never calls the loop's ``keep_running`` (callers count its calls); the loop stops it.

``stop`` waits only ``JOIN_S`` for a running pass, so a slow pass can outlive its loop. Passes are serialized per
process (``_PASS_LOCK``): a restarted loop's runner refuses requests while a stopped runner's pass still runs, and
never runs ``reconcile_links`` alongside it (both would share ``kanban_sync_client._LINKS_DONE``). A request refused
because another runner holds the lock logs one warning per hold: a hung pass would otherwise refuse every later pass
in the process silently.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger("hermes_cli.kanban_sync_client")

# Held for the length of one ``reconcile_links`` call by whichever runner (live, or stopped but still running) owns it.
_PASS_LOCK = threading.Lock()
# (runner, monotonic acquire time, hold token) while a pass holds ``_PASS_LOCK``, and the token of the last hold a
# refusal warned about: a wedged pass warns once, not on every refused request.
_HOLD: Optional[tuple[LinksPass, float, object]] = None
_WARNED_HOLD: Optional[object] = None


class LinksPass:
    JOIN_S = 5.0

    def __init__(self, gateway: str) -> None:
        self._gateway = gateway
        self._want = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._stopped = False
        self._boards: list[tuple[str, Path]] = []
        self._thread: Optional[threading.Thread] = None

    def request(self, boards: list[tuple[str, Path]]) -> bool:
        """Start a pass over ``boards`` unless one is running: this runner's, or an earlier loop's that outlived its
        ``stop``. True when it started."""
        if not self._idle.is_set():
            return False
        if _PASS_LOCK.locked():
            self._warn_held_elsewhere()
            return False
        self._boards = list(boards)
        self._idle.clear()
        self._want.set()
        if self._thread is None or not self._thread.is_alive():
            # The loop's contextvars (profile scope) cross into the thread; no agent.memory_provider dependency.
            self._thread = threading.Thread(target=contextvars.copy_context().run, args=(self._run,),
                                            name="kanban-sync-links", daemon=True)
            self._thread.start()
        return True

    def _warn_held_elsewhere(self) -> None:
        global _WARNED_HOLD
        hold = _HOLD
        if hold is None or hold[0] is self or hold[2] is _WARNED_HOLD:
            return
        _WARNED_HOLD = hold[2]
        _log.warning("kanban sync: chat links pass refused: another runner's pass has held the pass lock for %.0fs "
                     "(reconcile_links may be hung); later refusals stay quiet until it returns",
                     time.monotonic() - hold[1])

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        return self._idle.wait(timeout)

    def stop(self) -> None:
        self._stopped = True
        self._want.set()
        if self._thread is not None:
            self._thread.join(self.JOIN_S)

    def _pass(self, client) -> None:
        global _HOLD
        with _PASS_LOCK:  # a request that raced past ``request``'s check waits here, never overlaps
            _HOLD = (self, time.monotonic(), object())
            try:
                if not self._stopped:
                    client.reconcile_links(self._boards, self._gateway, skip_unchanged=True)
            finally:
                _HOLD = None

    def _run(self) -> None:
        from hermes_cli import kanban_sync_client as client  # module attribute: tests replace reconcile_links
        from hermes_cli.kanban_sync_connections import connection_scope
        with connection_scope():
            try:
                while not self._stopped:
                    if not self._want.wait(timeout=1.0):
                        continue
                    self._want.clear()
                    if self._stopped:
                        break
                    try:
                        self._pass(client)
                    except Exception as exc:  # health: allow BLE001 -- a failed pass must not end the thread
                        _log.warning("kanban sync: chat links pass failed (%s: %s)", type(exc).__name__, exc)
                    finally:
                        self._idle.set()
            finally:
                self._idle.set()
