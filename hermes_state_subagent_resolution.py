"""Owner-qualified evidence that a previously lost child has settled or was consumed. [daveey]"""
from __future__ import annotations

import json


def _child_tip_row(db, conn, sid: str) -> dict:
    """Follow child compression, not the conversation-only tip filter, on one snapshot.

    Inherited delegate markers do not make a continuation a new delegated fork. Use the
    canonical explicit-fork predicate so branch/reset/tool children cannot lend terminal proof.
    The writer uses this same walk before acknowledging, fencing concurrent rotations.
    """
    row = dict(conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone() or {})
    seen = {sid}
    while row.get("end_reason") == "compression":
        children = conn.execute("SELECT * FROM sessions WHERE parent_session_id=?"
                                " ORDER BY started_at DESC, id DESC", (row["id"],)).fetchall()
        tip = next((dict(child) for child in children if child["source"] == "subagent"
                    and not db._is_explicit_fork_child_row(dict(child), include_reset=True)), None)
        if not tip or tip["id"] in seen:
            break
        seen.add(tip["id"])
        row = tip
    return row


def resolved_subagents(db, ids: list[str], retained: list[dict]) -> set[str]:
    """Only terminal transcript proof or a delivered unit covering this child's last activity resolves loss.

    Read through the caller's store/profile, never a global ledger. Pending delivery (including requeue)
    and a foreign parent's unit are not acknowledgements. Retained IDs are read even outside inventory TTL.
    """
    import hermes_state_outcome as so
    from hermes_state_common import _placeholders
    from hermes_state_session_status_snapshot import _transcript_turn

    import time

    kept = [f["id"].removeprefix("subagent:") for f in retained if f.get("id", "").startswith("subagent:")]
    retained_sql = f" OR id IN ({_placeholders(kept)})" if kept else ""
    rows = db._read_all(
        "SELECT id FROM sessions"
        f" WHERE parent_session_id IN ({_placeholders(ids)}) AND source='subagent'"
        f" AND (COALESCE(ended_at, started_at) >= ?{retained_sql})",
        (*ids, time.time() - so.SUBAGENT_LOST_BOUND_S, *kept))
    resolved = set()
    by_id = {}
    for row in rows:
        sid = str(row["id"])
        with db._read_ctx() as conn:
            terminal = _child_tip_row(db, conn, sid)
        tip = terminal.get("id", sid)
        by_id[sid] = terminal
        receipt = db._read_one("SELECT value FROM state_meta WHERE key=?", (f"subagent_ack:{sid}",))
        if receipt is not None:
            ack = json.loads(receipt["value"])
            if ack["parent"] in ids and ack["at"] >= float(terminal.get("last_activity_at") or terminal.get("started_at") or 0):
                resolved.add(f"subagent:{sid}")
        if terminal.get("ended_at") is not None and terminal.get("end_reason") in {"agent_close", "completed"}:
            tail = so._without_finished_notes(so._tail(db, tip))
            if _transcript_turn(tail)["state"] == "completed":
                resolved.add(f"subagent:{sid}")
    exists = db._read_one("SELECT name FROM sqlite_master WHERE type='table' AND name='async_delegations'")
    if exists is None:
        return resolved
    units = db._read_all(
        "SELECT task_json, delivered_at FROM async_delegations"
        f" WHERE parent_session_id IN ({_placeholders(ids)}) AND delivery_state='delivered'"
        " AND delivered_at IS NOT NULL AND state NOT IN ('running','finalizing')", tuple(ids))
    for unit in units:
        task = json.loads(unit["task_json"] or "{}")
        for sid in (task.get("task_session_ids") or {}).values():
            if sid not in by_id:
                continue
            tip = by_id[sid]
            last = float(tip.get("last_activity_at") or tip.get("started_at") or 0)
            if float(unit["delivered_at"]) >= last:
                resolved.add(f"subagent:{sid}")
    return resolved


def acknowledge_subagents(db, parent: str, children: list[str], evidence: str) -> None:
    """Explicitly handle named lost children after independent verification, without declaring them successful."""
    import time
    import hermes_state_outcome as so

    if not isinstance(children, list) or not children or len(children) > 100 or any(
            not isinstance(sid, str) or not sid for sid in children):
        raise ValueError("name 1-100 exact subagent session IDs")
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2000:
        raise ValueError("subagent acknowledgement requires evidence (1-2000 characters)")
    ids = so._lineage_ids(db, so._tip(db, parent))

    def write(conn):
        receipts = []
        for sid in children:
            row = conn.execute("SELECT parent_session_id, source, ended_at, last_activity_at, started_at"
                               " FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None or row[0] not in ids or row[1] != "subagent":
                raise ValueError(f"not an owned subagent: {sid}")
            terminal = _child_tip_row(db, conn, sid)
            last = float(terminal.get("last_activity_at") or terminal.get("started_at") or 0)
            if terminal.get("ended_at") is None and last >= time.time() - so.SUBAGENT_LIVE_WINDOW_S:
                raise ValueError(f"subagent is still live: {sid}")
            receipts.append((f"subagent_ack:{sid}", json.dumps({"parent": row[0], "at": last,
                             "evidence": evidence.strip(), "acknowledged_at": time.time()})))
        conn.executemany("INSERT INTO state_meta (key,value) VALUES (?,?)"
                         " ON CONFLICT(key) DO UPDATE SET value=excluded.value", receipts)
    db._execute_write(write)


def checkpoint_delivery(conn, delegation_id: str) -> None:
    """Keep consumed-result proof past ledger retention; revoke it if admission is reopened.

    Called within the ledger's own transaction. Qualify children against their real parent.
    """
    row = conn.execute("SELECT parent_session_id, task_json, delivery_state, delivered_at"
                       " FROM async_delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
    if row is None:
        return
    parent, raw, state, at = row
    task = json.loads(raw or "{}")
    for sid in (task.get("task_session_ids") or {}).values():
        child = conn.execute("SELECT 1 FROM sessions WHERE id=? AND parent_session_id=? AND source='subagent'",
                             (sid, parent)).fetchone()
        if child is None:
            continue
        key = f"subagent_ack:{sid}"
        if state == "delivered" and at is not None:
            value = json.dumps({"parent": parent, "unit": delegation_id, "at": at})
            conn.execute("INSERT INTO state_meta (key,value) VALUES (?,?)"
                         " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        else:
            conn.execute("DELETE FROM state_meta WHERE key=? AND json_extract(value,'$.unit')=?",
                         (key, delegation_id))
