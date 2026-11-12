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
from typing import Any, Iterable

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
