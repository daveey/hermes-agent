"""One process-exit deadline for a gateway stop, tied to the supervisor's SIGKILL budget.

systemd SIGKILLs the gateway ``TimeoutStopSec`` after SIGTERM, and launchd does the same at the
live ``ExitTimeOut``. The shutdown watchdog in :mod:`gateway.shutdown_watchdog` covers only
``stop()``: it is disarmed when ``stop()`` returns, but the process still has a tail to run (the
cron/housekeeping thread waits in ``_start_gateway_shutdown_tail``, MCP shutdown, then the
default-executor join inside ``asyncio.run``, up to 300s). A slow tail was SIGKILLed with no stack
dump and no ledger stamp, so the next boot reported ``previous_unclean_exit``.

This module holds ONE deadline for the whole exit: ``signal time + supervisor budget - margin``.
It is armed by the stop-signal handler (and by ``stop()`` for non-signal stops), stays armed past
``stop()``, and is disarmed only by ``_exit_after_graceful_shutdown``. When it fires it dumps every
thread's stack, writes a ``gateway.exit_deadline`` exit-diag record with phase timings, stamps the
lifecycle ledger ``reason="exit_deadline"`` and ``os._exit``s before the supervisor's SIGKILL.

The tail and the routing saves read :func:`remaining_s` / :func:`is_exiting` so a normal stop
finishes inside the budget and the deadline only fires for a truly wedged tail.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Fire this long before the supervisor's SIGKILL so the dump + ledger stamp land. Short budgets
# (tests, tiny TimeoutStopSec) use a quarter of the budget instead.
EXIT_DEADLINE_MARGIN_S = 5.0
# Explicit budget override (seconds, before the margin). Used by tests and by supervisors the
# gateway cannot query.
BUDGET_ENV = "HERMES_GATEWAY_EXIT_BUDGET_S"
# state.db write patience for routing saves once the gateway is exiting: the default 20s patience
# made two saves eat 40s of a 70s TimeoutStopSec while a Desktop serve held the write lock.
DRAIN_DB_PATIENCE_S = 2.0

_lock = threading.Lock()
_done = threading.Event()
_state: dict[str, Any] = {}


def _reset_for_tests() -> None:
    with _lock:
        _state.clear()
        _done.clear()


def margin_for(budget_s: float) -> float:
    return min(EXIT_DEADLINE_MARGIN_S, max(0.0, budget_s) / 4.0)


def resolve_supervisor_stop_budget_s(
    launchd_exit_timeout_s: Optional[float] = None,
) -> tuple[Optional[float], str]:
    """Seconds between the stop signal and the supervisor's SIGKILL, plus where it came from.
    ``(None, "none")`` when no supervisor budget applies (foreground run, unknown supervisor)."""
    raw = os.environ.get(BUDGET_ENV, "").strip()
    if raw:
        with contextlib.suppress(ValueError):
            value = float(raw)
            if value > 0:
                return value, "env"
    if launchd_exit_timeout_s:
        with contextlib.suppress(TypeError, ValueError):
            if float(launchd_exit_timeout_s) > 0:
                return float(launchd_exit_timeout_s), "launchd"
    with contextlib.suppress(Exception):
        from gateway.shutdown_forensics import current_systemd_timeout_stop_s
        value = current_systemd_timeout_stop_s()
        if value:
            return value, "systemd"
    return None, "none"


def is_exiting() -> bool:
    """True once a stop armed the exit deadline (signal or ``stop()``)."""
    return bool(_state.get("armed"))


def remaining_s() -> Optional[float]:
    """Seconds left before the deadline fires; ``None`` when no deadline is set (yet)."""
    deadline = _state.get("deadline_mono")
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


# Left on the deadline after the cron/housekeeping waits: MCP shutdown (5s) + the exit funnel.
TAIL_RESERVE_S = 7.0


def clamp_wait(default_s: float, reserve_s: float = TAIL_RESERVE_S, *, phase: str = "") -> float:
    """``default_s`` capped to the time left minus ``reserve_s`` (for the steps after it).
    ``phase`` marks the start of the wait for the exit-diag phase timings."""
    if phase:
        mark_phase(phase)
    left = remaining_s()
    if left is None:
        return default_s
    return max(0.0, min(default_s, left - reserve_s))


def mark_phase(name: str) -> None:
    """Record ``name`` at its elapsed time since the stop began (for the exit-diag record)."""
    started = _state.get("started_mono")
    if started is None:
        return
    with _lock:
        _state.setdefault("phases", {})[name] = round(time.monotonic() - started, 3)


def arm(
    reason: str, *, launchd_exit_timeout_s: Optional[float] = None,
    min_budget_s: Optional[float] = None,
    snapshot_fn: Optional[Callable[[], dict[str, Any]]] = None,
    exit_code: int = 1,
) -> bool:
    """Arm the exit deadline once per process; later calls only add a phase mark. Never raises.

    The budget is resolved on the watchdog thread (``systemctl show`` is a subprocess and this is
    called from the event loop's signal handler); until it resolves, :func:`remaining_s` is None
    and waits keep their defaults. ``min_budget_s`` keeps a non-signal stop (in-band restart, not
    supervisor-timed) from getting a shorter leash than its configured drain.
    """
    started = time.monotonic()
    with _lock:
        if _state.get("armed"):
            _state.setdefault("phases", {}).setdefault(reason, round(started - _state["started_mono"], 3))
            return False
        if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get(BUDGET_ENV):
            return False  # in-process stop() tests must never get a delayed os._exit
        _state.update(armed=True, reason=reason, started_mono=started,
                      started_at=datetime.now(UTC).isoformat(), phases={reason: 0.0})

    def _watch() -> None:
        budget, source = resolve_supervisor_stop_budget_s(launchd_exit_timeout_s)
        if min_budget_s is not None and (budget is None or budget < min_budget_s) and source != "env":
            budget, source = float(min_budget_s), f"{source}+min" if budget else "min"
        if budget is None:
            logger.debug("Exit deadline: no supervisor stop budget; not enforcing one")
            return
        deadline = started + budget - margin_for(budget)
        with _lock:
            _state.update(budget_s=budget, budget_source=source, deadline_mono=deadline)
        logger.info("Exit deadline armed (%s): %.0fs supervisor budget from %s, fires at +%.1fs",
                    reason, budget, source, deadline - started)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if _done.wait(timeout=min(left, 1.0)):
                return
        if _done.is_set():
            return
        _fire(exit_code, snapshot_fn)

    try:
        threading.Thread(target=_watch, daemon=True, name="gateway-exit-deadline").start()
    except Exception:
        logger.debug("Failed to start exit deadline thread", exc_info=True)
        return False
    return True


def arm_for_signal(runner: Any) -> None:
    """Stop-signal handler hook: start the deadline at the signal, with the supervisor budget."""
    arm("signal", launchd_exit_timeout_s=getattr(runner, "_launchd_exit_timeout_s", None))


def arm_for_stop(runner: Any, stop_leash_s: float, snapshot_fn: Callable[[], dict[str, Any]]) -> None:
    """``stop()`` hook. A signal stop armed the deadline already (this only marks the phase); a
    non-signal stop (in-band restart, not supervisor-timed) gets at least its own stop() leash
    plus the tail, so it is never cut shorter than before."""
    by_signal = getattr(runner, "_stop_requested_by_signal", False)
    arm("stop", launchd_exit_timeout_s=getattr(runner, "_launchd_exit_timeout_s", None),
        min_budget_s=None if by_signal else stop_leash_s + TAIL_RESERVE_S, snapshot_fn=snapshot_fn)


def disarm() -> None:
    """Called by the graceful exit funnel just before ``os._exit``."""
    _done.set()


def _exit_diag_record() -> dict[str, Any]:
    with _lock:
        return {
            "reason": _state.get("reason"),
            "budget_s": _state.get("budget_s"),
            "budget_source": _state.get("budget_source"),
            "margin_s": margin_for(_state.get("budget_s") or 0.0),
            "started_at": _state.get("started_at"),
            "elapsed_s": round(time.monotonic() - _state.get("started_mono", time.monotonic()), 3),
            "phases": dict(_state.get("phases") or {}),
        }


def _fire(exit_code: int, snapshot_fn: Optional[Callable[[], dict[str, Any]]]) -> None:
    """Dump stacks, record, stamp the ledger, hard-exit. Every step is best-effort."""
    record = _exit_diag_record()
    if snapshot_fn is not None:
        try:
            record["snapshot"] = snapshot_fn()
        except Exception as exc:  # health: allow BLE001 -- forensic snapshot of a wedged process; recorded, never raised
            record["snapshot"] = {"snapshot_error": repr(exc)}
    from gateway.shutdown_watchdog import (
        _hard_exit, _mark_exited_quietly, get_shutdown_watchdog_dump_path)
    with contextlib.suppress(Exception):
        import faulthandler
        dump_path = get_shutdown_watchdog_dump_path()
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        header = {"event": "exit_deadline_fired", "pid": os.getpid(),
                  "fired_at": datetime.now(UTC).isoformat(), **record}
        with open(dump_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(header, default=str) + "\n--- faulthandler dump (all threads) ---\n")
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)
            fh.write("--- end dump ---\n")
    with contextlib.suppress(Exception):
        sys.stderr.write(
            f"Gateway exit deadline fired {record['elapsed_s']:.1f}s after stop "
            f"(budget {record['budget_s']}s from {record['budget_source']}); forcing exit.\n")
        sys.stderr.flush()
    with contextlib.suppress(Exception):
        from hermes_cli.gateway_exit_diag import make_exit_diag
        make_exit_diag()("gateway.exit_deadline", **record)
    with contextlib.suppress(Exception):
        logger.critical("Exit deadline fired %.1fs after stop (budget %ss from %s, phases %s); "
                        "forcing exit before the supervisor's SIGKILL", record["elapsed_s"],
                        record["budget_s"], record["budget_source"], record["phases"])
    with contextlib.suppress(Exception):
        from gateway.status import release_gateway_runtime_lock, remove_pid_file
        remove_pid_file()
        release_gateway_runtime_lock()
    with contextlib.suppress(Exception):
        from hermes_logging import drain_log_queue
        drain_log_queue(timeout=1.0)
    _mark_exited_quietly(exit_code, "exit_deadline")
    _hard_exit(exit_code)
