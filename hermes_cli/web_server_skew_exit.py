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
# Two consecutive observations: a checkout mid-rewrite (ref file replaced, packed-refs repacked) can
# briefly read differently; a real update reads the new revision on every poll after it finishes.
_CONFIRMATIONS = 2

# ``(boot_rev, disk_rev)`` when the checkout drifted since boot, else None (``detect_code_skew``).
Skew = Optional[tuple[str, str]]


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
                             on_retire: Optional[Callable[[str], None]] = None) -> threading.Thread:
    """Daemon thread shared by the SSH-isolated retirement watchdogs: ``observe`` returns the log
    reason while retirement is warranted (else None); after ``_CONFIRMATIONS`` consecutive reasons
    the backend exits only through the retirement fence (proven idle, admission closed first)."""
    if fence is None:
        from hermes_cli.backend_retirement import retirement

        fence = retirement

    def _loop() -> None:
        seen = polls = 0
        while not getattr(server, "should_exit", False) and (max_polls is None or polls < max_polls):
            polls += 1
            reason = observe()
            seen = seen + 1 if reason else 0
            if reason and seen >= _CONFIRMATIONS:
                permit = fence.prepare()
                if permit.get("ok") and fence.commit(permit.get("token")).get("ok"):
                    _log.warning("%s", reason)
                    if on_retire is not None:
                        on_retire(reason)
                    server.should_exit = True
                    return
            time.sleep(poll_s)

    thread = threading.Thread(target=_loop, daemon=True, name=thread_name)
    thread.start()
    return thread


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

    return _run_retirement_watchdog(server, observe=_observe, fence=fence, poll_s=poll_s,
                                    max_polls=max_polls, thread_name="ssh-isolated-skew-watchdog",
                                    on_retire=_on_retire)
