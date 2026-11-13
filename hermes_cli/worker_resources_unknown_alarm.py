"""An agent repair card when ``worker_resources_unknown`` holds every claim on this worker. [daveey]

t_01a1d5bf526e1344 (2026-11-13): worker-1 spawned nothing for 17 min. A state-suite shard's board under
``/dev/shm`` stayed registered in the account worker inventory after the run deleted it, so every
``refusal()`` failed closed as ``worker_resources_unknown``. The "dispatcher stuck" WARNING only logged it;
the record was moved by hand.

``refusal()`` is host-wide: its inventory and policy are the account's, so while it answers unknown no
claim on this host can pass. :func:`observe` keeps that streak in ``<policy root>/worker-resources/
unknown.json`` (first seen, last seen, the failing check, the registered record it names), rewritten on a
new streak, a changed detail or every :data:`REFRESH_SECONDS`, and removed by the first other answer. The
dispatcher sweep (:func:`sweep`) opens ONE agent repair card per host per incident once a fresh streak is
older than :data:`STALL_SECONDS`, placed off this host (``kanban_pickup_stall.alarm_placement``), naming the
record path. A streak whose last observation is older than :data:`GAP_SECONDS` (nothing tried to claim)
is a new incident. No Inbox post, no human ask.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

STALL_SECONDS = 600
REFRESH_SECONDS = 60
GAP_SECONDS = 300
KEY_PREFIX = "worker-resources-unknown:"


def _path() -> Path:
    from hermes_cli import worker_compute_capacity as capacity
    return capacity.policy_root() / "worker-resources" / "unknown.json"


def record_for(exc: Optional[BaseException]) -> Optional[str]:
    """The inventory record naming a registered board that vanished, or None."""
    name = getattr(exc, "filename", None)
    if not isinstance(exc, FileNotFoundError) or not name or not os.path.isabs(str(name)):
        return None
    from hermes_cli import worker_compute_capacity as capacity
    digest = hashlib.sha256(str(name).encode()).hexdigest()
    return str(capacity.policy_root() / "worker-resources" / "boards" / f"{digest}.json")


def read() -> Optional[dict[str, Any]]:
    try:
        state = json.loads(_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and isinstance(state.get("since"), (int, float)) else None


def _write(state: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(state, handle)
    os.replace(handle.name, path)


def observe(unknown: bool, exc: Optional[BaseException] = None, *, now: Optional[float] = None) -> None:
    """Record one ``refusal()`` answer. Never raises: the claim gate must not depend on its alarm."""
    try:
        now = time.time() if now is None else now
        state: Optional[dict[str, Any]] = read()
        if not unknown:
            if state is not None:
                _path().unlink(missing_ok=True)
            return
        detail = f"{type(exc).__name__}: {exc}" if exc is not None else "occupancy or max_in_progress unreadable"
        if state is None or now - float(state.get("last", 0)) > GAP_SECONDS:
            state = {"since": now}
        elif state.get("detail") == detail and now - float(state["last"]) < REFRESH_SECONDS:
            return
        state = {**state, "last": now, "detail": detail, "record": record_for(exc)}
        _write(state)
    except Exception:  # health: allow BLE001 -- alarm bookkeeping must never change the claim answer
        _log.debug("worker_resources_unknown streak not recorded", exc_info=True)


def sweep(conn, *, now: Optional[float] = None) -> Optional[str]:
    """The repair card id covering a stalled streak, or None."""
    state = read()
    now = time.time() if now is None else now
    if state is None or now - float(state.get("last", 0)) > GAP_SECONDS:
        return None
    since = float(state["since"])
    if now - since <= STALL_SECONDS:
        return None
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_pickup_stall as stall
    from hermes_cli import kanban_worker_node as wn
    if sync.board_mode(conn) != "primary":
        return None
    own = wn._own(conn)
    minutes = int((now - since) // 60)
    record = state.get("record")
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since))
    target = f"record {record}" if record else "no registered record named"
    body = (f"Since {started} ({minutes} min) every worker_resource_admission.refusal() on {own} answered "
            f"`worker_resources_unknown`, so no card can claim there (threshold {STALL_SECONDS} s).\n\n"
            f"Failing check: `{state.get('detail')}`\nInventory {target}.\n\n"
            f"On {own}: read the record (`cat <record>`), check whether its board or directory still exists and "
            "which process registered it (a test or scratch harness writing the real account inventory is a bug "
            "to fix at its source). A record whose board is gone for good may be moved out under the account "
            "admission lock (`hermes_cli.admission_fence.admission_lock(<account>/.hermes)`); keep a copy as "
            f"evidence. Verify `refusal()` returns None and {own} claims again. This is agent-owned repair, not "
            "a human decision.")
    key = f"{KEY_PREFIX}{own}:"
    now_ms = int(now * 1000)
    card = stall._covering_repair(conn, key, now_ms)  # one card per host per incident
    if card is None:
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_triage_sweep import _supervisor_profile
        reg = wn.registry()
        assignee = _supervisor_profile()
        # Never pinned here: this host is the one claiming nothing.
        runs_on, why = stall.alarm_placement(own, None if reg is None else (reg.get("machines") or {}),
                                             assignee=assignee, now_ms=now_ms)
        card = kb.create_task(
            conn, title=(f"worker_resources_unknown holds every claim on {own} for {minutes} min: "
                         f"{record or state.get('detail')}")[:240],
            body=body + f"\n\nPlaced with runs_on {runs_on}: {why}.", assignee=assignee,
            created_by=stall.DETECTOR, workspace_kind="scratch", requires=list(stall._REQUIRES),
            idempotency_key=f"{key}{now_ms}", runs_on=runs_on, priority=-1)
        _log.warning("worker_resources_unknown on %s for %d min: repair card %s", own, minutes, card)
    return card


def safe_sweep(conn) -> Optional[str]:
    try:
        return sweep(conn)
    except Exception:  # health: allow BLE001 -- the detector must never stop dispatch
        _log.warning("kanban dispatcher: worker_resources_unknown sweep failed", exc_info=True)
        return None
