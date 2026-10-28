"""Why ``start_gateway()`` refused to start: another gateway already owns this home or host.

A refused second start exits 1 like a crash, so ``hermes gateway run`` logs it as
``gateway.start_refused`` with the reason and the owner's pid + liveness instead of a bare
``gateway.exit_nonzero`` (self-heal filed a refused duplicate next to a live gateway as "died
uncleanly", MacBook Oct 2026, defect gateway-6efc15a270). Reasons: ``already_running`` (a live
gateway under this HERMES_HOME), ``pid_claim`` (lost the runtime lock / PID file; ``detail`` says
which step) and ``host_refused`` (the host gateway refuses a second one). Process-wide: one start
per process.
"""

from __future__ import annotations

from typing import Optional

_last_start_refusal: Optional[dict] = None


def note_start_refused(reason: str, owner_pid: Optional[int] = None, detail: Optional[str] = None) -> None:
    """Record why this start was refused; ``owner_alive`` is None when no owner pid is known."""
    global _last_start_refusal
    owner_alive: Optional[bool] = None
    if owner_pid is not None:
        from gateway.status import _pid_exists
        try:
            owner_alive = _pid_exists(owner_pid)
        except Exception:  # health: allow BLE001 -- a liveness probe must not turn a refusal into a crash
            owner_alive = None
    _last_start_refusal = {"reason": reason, "owner_pid": owner_pid, "owner_alive": owner_alive}
    if detail:
        _last_start_refusal["detail"] = detail


def reset_start_refusal() -> None:
    global _last_start_refusal
    _last_start_refusal = None


def last_start_refusal() -> Optional[dict]:
    """``{"reason", "owner_pid", "owner_alive"[, "detail"]}`` for a refused start, else None."""
    return _last_start_refusal
