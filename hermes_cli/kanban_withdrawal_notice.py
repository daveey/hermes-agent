"""Explicit, content-free owner withdrawal receipts for remote transport."""
from __future__ import annotations

import json
import math


def publish(conn, task: dict, *, latest: tuple | None = None) -> dict | None:
    """Read inside the collector snapshot; never infer withdrawal from status.

    ``latest`` is ``(row,)`` when the caller already read this card's latest ``request_withdrawn`` event
    ``(id, created_at, payload)`` in this snapshot (row None when it has none)."""
    if task.get("status") != "archived":
        return None
    row = latest[0] if latest is not None else conn.execute(
        "SELECT id,created_at,payload FROM task_events WHERE task_id=? "
        "AND kind='request_withdrawn' ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row[2] or "{}")
    except (TypeError, ValueError):
        return None
    if (not isinstance(payload, dict) or type(payload.get("notice_version")) is not int
            or payload["notice_version"] != 1):
        return None
    return {"event_id": row[0], "kind": "request_withdrawn", "created_at": row[1]}


def envelope(task: dict) -> dict | None:
    """Fail closed for malformed/other-state envelopes at the replica boundary."""
    notice = task.get("withdrawal_notice")
    if task.get("status") != "archived" or not isinstance(notice, dict):
        return None
    if (set(notice) != {"event_id", "kind", "created_at"}
            or notice.get("kind") != "request_withdrawn"
            or type(notice.get("event_id")) is not int or notice["event_id"] <= 0
            or type(notice.get("created_at")) not in (int, float)
            or not math.isfinite(notice["created_at"]) or notice["created_at"] <= 0):
        return None
    return notice
