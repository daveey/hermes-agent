"""Kanban notices refused only by the core update's admission hold wake promptly once it lifts. [daveey]

A notice wake that finds the chat ``"busy"`` because a core update holds admission (``admission_fence``,
``holds/hermes-sync``) is not a busy chat: nothing will be running there once the hold lifts. Before this, the sweep
wrote the passive row ("busy, notice row written") and the notice guarantee offered it again only after ``GRACE_S``
(300 s) and then ``BUSY_RETRY_S`` doubling, so notices kept waiting minutes after the update was done (hub,
2026-11-21). Now such a row is stamped ``closeout = {state: "deferred", hold_wait: true}`` (``hold_wait_closeout``),
which the guarantee treats as due as soon as the hold is gone (no grace, no busy attempt charged), and a watcher
thread polls the hold every ``POLL_S`` while such rows exist: within ``POLL_S`` of the hold lifting it runs the
guarantee pass, then again every ``PASS_EVERY_S`` while wakes were left waiting for a slot, for at most
``DRAIN_S``. The pass's wake slots (``wake_slots``: 8 for the host-loop holder) bound the rate; every other
refusal keeps its backoff.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

POLL_S = 2.0
PASS_EVERY_S = 5.0
DRAIN_S = 120.0
# The durable fallback when no watcher survives (this backend restarted): the row is due by time anyway.
FALLBACK_RETRY_S = 300.0

_mu = threading.Lock()
_state: dict[str, Any] = {"waiting": False, "thread": None}


def update_hold_active(home=None) -> bool:
    """True while the install's core update holds admission (the marker ``admission_fence.reserve`` refuses on).
    An unreadable marker answers False: the caller then keeps its ordinary busy backoff."""
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home
        from hermes_cli.admission_fence import is_held
        return bool(is_held(get_default_hermes_root(home=home or get_hermes_home()) / "core-update"))
    except Exception:  # health: allow BLE001 -- unknown is "not held": today's backoff applies; logged with traceback
        logger.debug("update admission hold unreadable", exc_info=True)
        return False


def hold_wait_closeout(now: Optional[float] = None) -> dict:
    """The ``closeout`` stamp of a notice refused only by the update hold."""
    now = time.time() if now is None else now
    return {"state": "deferred", "hold_wait": True, "retry_at": now + FALLBACK_RETRY_S, "at": now}


def is_hold_wait(meta: Any) -> bool:
    closeout = meta.get("closeout") if isinstance(meta, dict) else None
    return isinstance(closeout, dict) and closeout.get("state") == "deferred" and closeout.get("hold_wait") is True


def _default_pass() -> list:
    from tui_gateway.notice_guarantee import guarantee_pass
    return guarantee_pass()


def note_waiting(*, run_pass: Optional[Callable[[], list]] = None, held: Callable[[], bool] = update_hold_active,
                 sleep: Callable[[float], None] = time.sleep, spawn: Optional[Callable[[Callable], Any]] = None) -> bool:
    """A notice now waits for the hold: make sure the watcher runs. True when this call started it."""
    with _mu:
        _state["waiting"] = True
        if _state["thread"] is not None:
            return False
        _state["thread"] = True

    def job() -> None:
        try:
            watch(run_pass=run_pass or _default_pass, held=held, sleep=sleep)
        except BaseException:
            with _mu:  # a normal return already cleared it, under the lock that decided to return
                _state["thread"] = None
            raise

    if spawn is None:
        from agent.memory_provider import spawn_context_thread
        spawn_context_thread(job, name="notice-hold-wait").start()
    else:
        spawn(job)
    return True


def watch(*, run_pass: Callable[[], list], held: Callable[[], bool], sleep: Callable[[float], None],
          monotonic: Callable[[], float] = time.monotonic) -> int:
    """Wait for the hold to lift, then run guarantee passes until nothing waits for a slot. Returns the pass count."""
    passes = 0
    while True:
        while held():
            sleep(POLL_S)
        with _mu:
            _state["waiting"] = False
        started = monotonic()
        while True:
            try:
                results = run_pass() or []
            except Exception:  # health: allow BLE001 -- watcher thread boundary; the sweeper's pass retries
                logger.warning("notice hold-wait pass failed", exc_info=True)
                results = []
            passes += 1
            waiting_slots = sum(1 for r in results if isinstance(r, dict) and r.get("result") == "skipped:slots")
            logger.info("notice hold-wait: update hold lifted; pass %d woke %d, %d still waiting for a slot", passes,
                        sum(1 for r in results if isinstance(r, dict) and r.get("result") == "dispatched"),
                        waiting_slots)
            if not waiting_slots or monotonic() - started >= DRAIN_S or held():
                break
            sleep(PASS_EVERY_S)
        with _mu:
            if not _state["waiting"]:
                # Cleared in the same locked section that decides to return: a ``note_waiting`` after this
                # starts a new watcher, one before it sets ``waiting`` and this one watches again.
                _state["thread"] = None
                return passes
        # Another notice was refused by a new hold meanwhile: watch again.


def _reset_for_tests() -> None:
    with _mu:
        _state.update(waiting=False, thread=None)
