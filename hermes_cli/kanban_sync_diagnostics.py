"""Durable, gateway-scoped evidence for Board claim stalls."""
from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync


def _last_claim(conn, task_id, gateway):
    return conn.execute(
        "SELECT payload, created_at FROM task_events WHERE task_id=? AND kind='claim_deferred' "
        "AND json_extract(payload, '$.gateway')=? ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id, gateway)).fetchone()


def record_claim(conn, task_id: str, reason: str) -> None:
    """Keep changed outcomes after a missing-row denial, not an event on every dispatcher tick."""
    state = sync.sync_state(conn)
    gateway = state["gateway_id"]
    with kb.write_txn(conn):
        last = _last_claim(conn, task_id, gateway)
        if last is None and reason != "not_found":
            return
        from hermes_cli.kanban_parent_unclaimed import ready_since
        if (last is not None and last["created_at"] >= ready_since(conn, task_id)
                and kb._json_dict(last["payload"]).get("reason") == reason):
            return
        kb._append_event(conn, task_id, "claim_deferred", {"reason": reason, "gateway": gateway})


def update_hold() -> dict:
    """Use admission's live marker reader, not an independent existence-only diagnosis."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from hermes_cli.kanban_gateway_drain import core_update_hold
    hold = core_update_hold()
    if hold is None:
        return {}
    try:
        started = datetime.fromtimestamp(float(hold["started_at"]), ZoneInfo("America/Los_Angeles"))
        stamp = started.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        stamp = "at an unknown time"
    return {"core_update_hold": True, "core_update_started": stamp}


def missing_board(conn, task_id: str, since: int) -> dict:
    """Only this gateway's latest claim in this ready episode can diagnose a missing Board row."""
    state = sync.sync_state(conn)
    if not state or state.get("mode") != "primary":
        return {}
    last = _last_claim(conn, task_id, state["gateway_id"])
    if last is None or last["created_at"] < since or kb._json_dict(last["payload"]).get("reason") != "not_found":
        return {}
    rejected = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='sync_rejected' "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
    reason = kb._json_dict(rejected[0]).get("reason") if rejected else None
    return {"missing_shared_board": True, "sync_reject_reason": reason or "no sync rejection recorded"}
