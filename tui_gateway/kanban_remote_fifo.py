"""Remote Kanban notices the canonical gateway's FIFO holds for a chat, read from state.db. [daveey]

``gateway.kanban_remote_owner`` admits a remote card's notice into the session FIFO of a busy chat the
gateway owns; the admission's descriptor carries the notice's ``kanban_event_keys`` until its turn
writes the ``kanban_notice`` user row. Both consumers read that here, in any process on this host:
the gateway (a successor adopts a pending admission instead of admitting it twice) and the Desktop
backend's sweep (``kanban_remote_notices._deliver_link``), which leaves a notice the FIFO holds to the
gateway instead of claiming it beside it (t_01a1cff0541bf30c).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_PENDING = ("queued", "started", "unknown")


def lineage_ids(db: Any, *ids: str) -> set[str]:
    """``ids`` plus every session of their compression lineage. A read error keeps ``ids``."""
    out = {str(i) for i in ids if i}
    read_ctx = getattr(db, "_read_ctx", None)
    if read_ctx is None:
        return out
    try:
        from hermes_state_sessions import _LINEAGE_CTE_SQL
        with read_ctx() as conn:
            for sid in sorted(out):
                out.update(str(row[0]) for row in conn.execute(_LINEAGE_CTE_SQL + " SELECT id FROM lineage",
                                                                (sid, sid)))
    except sqlite3.Error:
        logger.debug("remote kanban fifo: lineage read failed", exc_info=True)
    return out


def _descriptor_keys(payload_json: Any) -> list[str]:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    descriptor = payload.get("local_automation_v1") or (payload.get("native_text_v1") or {}).get("automation") or {}
    keys = descriptor.get("kanban_event_keys") if isinstance(descriptor, dict) else None
    return [str(k) for k in keys if k] if isinstance(keys, list) else []


def pending_keys(db: Any, ids: Iterable[str]) -> set[str]:
    """Notice keys of admissions still pending in the FIFO of any of ``ids``. A read error answers empty."""
    wanted = sorted({str(i) for i in ids if i})
    read_ctx = getattr(db, "_read_ctx", None)
    if not wanted or read_ctx is None:
        return set()
    marks = ",".join("?" * len(wanted))
    try:
        with read_ctx() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM session_admissions WHERE target_session_id IN ({marks}) "
                f"AND status IN ({','.join('?' * len(_PENDING))}) AND payload_json LIKE '%kanban_event_keys%'",
                (*wanted, *_PENDING)).fetchall()
    except sqlite3.Error:
        logger.debug("remote kanban fifo: admission read failed", exc_info=True)
        return set()
    return {key for row in rows for key in _descriptor_keys(row[0])}


def gateway_holds(db: Any, ids: Iterable[str], event_key: str) -> bool:
    """Whether the Desktop backend must leave this notice to the gateway: a live gateway admission holds
    its key. Only then: a dropped admission hands the notice back to this sweep (t_01a1cff0541bf30c
    amendment), and a busy chat's claim here is released within the same pass."""
    return bool(event_key) and event_key in pending_keys(db, ids)


def canonical_gateway_pid(profile: Optional[str]) -> Optional[int]:
    """The verified live gateway that runs ``profile``'s chats: the gateway of that profile's own home, else
    the default root's (a multiplexed gateway serves the profile from there). Identity is the start-time and
    command-line proven pid of ``gateway.status.live_gateway_pid_for_home``, never the control socket's identify
    call (``discover_owner`` drops the pid and times out under exactly the load this guards). None when unproven."""
    try:
        from gateway.status import live_gateway_pid_for_home
        from hermes_cli.profiles import get_profile_dir
        pid = live_gateway_pid_for_home(get_profile_dir(profile or "default"))
        if pid is None:
            from hermes_cli.gateway_multiplex_served import live_default_gateway_pid
            pid = live_default_gateway_pid()
        return int(pid) if pid is not None else None
    except Exception:  # health: allow BLE001 -- an unproven gateway keeps the sweep's own path
        logger.debug("remote kanban fifo: gateway pid lookup failed", exc_info=True)
        return None


def gateway_lease_pid(db: Any, ids: Iterable[str], profile: Optional[str], now: Optional[float] = None,
                      gateway_pid=None) -> Optional[int]:
    """The canonical gateway's pid when it holds an UNEXPIRED turn lease on the chat (``ids``: its lineage), else
    None. The Desktop backend's sweep then leaves the notice alone: no claim, no wake, no passive row.

    Why standing aside gives up nothing (t_01a1cff0541bf30c G3, review t_01a1d4c44f637fff): while another live
    process holds an unexpired lease, the sweep can deliver nothing. Its passive row is refused
    (``record_kanban_notice``'s ``reject_active_turn_lease`` guard raises ``SessionTurnLeaseLostError``) and its
    wake is refused (``refuses_held_lease_at_once`` for a desktop/tui turn). Claiming anyway only logged
    "deferred: a turn is running" every pass and held the claim the gateway's own pass needs. Once the lease is
    released, expires (the passive-row guard reclaims an expired lease even from a live pid, so this answers None
    then) or its holder dies, the sweep runs as before; the gateway's owner pass meanwhile admits the notice to
    the chat's FIFO. Lease-only on purpose: a ledger arm (``queued``/``started`` admissions) would also hold the
    notice between turns, when the passive row could land and the owner pass may not run (notify_in_gateway off,
    a non-launch profile, the restore gap)."""
    owner_of = getattr(db, "session_turn_lease_owner", None)
    if owner_of is None:
        return None
    now = time.time() if now is None else now
    holders = []
    try:
        for sid in sorted({str(i) for i in ids if i}):
            row = owner_of(sid) or {}
            holder = str(row.get("holder") or "")
            if holder and float(row.get("expires_at") or 0) > now:
                holders.append(holder)
    except (sqlite3.Error, TypeError, ValueError):
        logger.debug("remote kanban fifo: lease read failed", exc_info=True)
        return None
    if not holders:
        return None  # the common case: no running turn, no gateway probe
    from tui_gateway.kanban_leg_custody import gateway_owner
    pid = canonical_gateway_pid(profile) if gateway_pid is None else gateway_pid
    if pid is None:
        return None
    owner = gateway_owner(tuple(ids), None, tuple(dict.fromkeys(holders)), gateway_pid=pid, launch_profile="")
    return owner.pid if owner is not None else None
