"""Bind status to lifecycle evidence in its capture transaction, never at send. [daveey]

Legacy/autocommit rows stay NULL. A transaction with multiple status captures for
one task is deliberately unbound: the final event cannot safely identify every
intermediate intent. Those rows retain legacy sending rather than invent origin.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

_log = logging.getLogger(__name__)
_TABLE = "kanban_status_capture"
_LIMIT = 512


def arm(conn: sqlite3.Connection) -> bool:
    """Record new outbox status ops on this connection; savepoint rollback is native."""
    try:
        cursor = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_outbox'")
        if cursor is None or not cursor.fetchone():
            return False
        # write_txn also serves boundary-only connection adapters and minimal
        # revision stores. SQLite accepts invalid NEW.column trigger references
        # at CREATE time, then breaks an unrelated later mutation: check first.
        required = {"tasks": {"id", "status"},
                    "kanban_sync_outbox": {"op_id", "row_uid", "table_name", "op", "fields", "status_origin"}}
        from hermes_cli.kanban_sync_apply_schema import table_columns
        if any(not needed <= set(table_columns(conn, table)) for table, needed in required.items()):
            return False
        conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_TABLE} (op_id TEXT PRIMARY KEY, task_id TEXT)")
        conn.execute(f"DELETE FROM temp.{_TABLE}")
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS kanban_status_changes (task_id TEXT PRIMARY KEY, status TEXT, n INTEGER)")
        conn.execute("DELETE FROM temp.kanban_status_changes")
        for action, clause in (("INSERT", ""), ("UPDATE OF status", "WHEN OLD.status IS NOT NEW.status")):
            name = "insert" if action == "INSERT" else "update"
            conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS kanban_status_change_{name} AFTER {action} ON main.tasks "
                         f"{clause} BEGIN INSERT INTO kanban_status_changes VALUES (NEW.id, NEW.status, 1) "
                         "ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, n=n+1; END")
        conn.execute(f"CREATE TEMP TRIGGER IF NOT EXISTS kanban_status_capture_trg "
                     "AFTER INSERT ON main.kanban_sync_outbox "
                     "WHEN NEW.table_name='tasks' AND NEW.op='upsert' "
                     "AND json_type(NEW.fields,'$.status') IS NOT NULL BEGIN "
                     f"INSERT INTO {_TABLE} VALUES (NEW.op_id, NEW.row_uid); END")
        from hermes_cli import kanban_sync_evidence_order
        kanban_sync_evidence_order.arm(conn)
        return True
    except sqlite3.Error:  # unsynced/pre-schema connection: no new protocol assertion
        return False


def current_origin(conn: sqlite3.Connection, task: str) -> str | None:
    """Only callers capturing a NEW status intent may use this (never a retry)."""
    from hermes_cli.kanban_sync_divergence import _event_order
    from hermes_cli.kanban_terminal_guard import _events, is_reopen, terminal_status

    events = [e for e in _events(conn, task) if terminal_status(e) or is_reopen(e)]
    if not events:
        return ""
    uid = max(events, key=_event_order).get("uid")
    return uid if isinstance(uid, str) and uid else None


def bind(conn: sqlite3.Connection, armed: bool) -> None:
    """Before outer COMMIT, including its own row-before-event transition evidence."""
    if not armed:
        return
    try:
        rows = conn.execute(f"SELECT op_id, task_id FROM temp.{_TABLE} LIMIT ?", (_LIMIT + 1,)).fetchall()
        if len(rows) > _LIMIT:
            _log.warning("kanban status capture exceeds %d; retaining legacy envelopes", _LIMIT)
            return
        from hermes_cli import kanban_sync_evidence_order
        kanban_sync_evidence_order.bind(conn, [tuple(r) for r in rows])
        counts: dict[str, int] = {}
        for _op, task in rows:
            counts[task] = counts.get(task, 0) + 1
        from hermes_cli.kanban_home_transfer import frozen
        for op, task in rows:
            if frozen(conn, task):
                continue  # source preparation is a durable mutation fence
            change = conn.execute("SELECT status, n FROM temp.kanban_status_changes WHERE task_id=?", (task,)).fetchone()
            if not change:
                continue  # repair/requeue did not capture a new status intent
            captured = conn.execute("SELECT json_extract(fields,'$.status') FROM kanban_sync_outbox WHERE op_id=?", (op,)).fetchone()
            known = counts[task] == 1 and change[1] == 1 and captured and captured[0] == change[0]
            origin = current_origin(conn, task) if known else None
            if known:
                from hermes_cli.kanban_owner_reassessment_transport import captured_origin
                ordinary = origin  # a synthesized parent never carries a park uid without its receipt
                origin = captured_origin(conn, op, origin)
                conn.execute("UPDATE kanban_sync_outbox SET status_origin=? WHERE op_id=? AND wire_json IS NULL",
                             (origin, op))
                origin = ordinary
            conn.execute("INSERT INTO kanban_sync_status_origins(task_id, origin, status) "
                         "SELECT id, ?, status FROM tasks WHERE id=? ON CONFLICT(task_id) "
                         "DO UPDATE SET origin=excluded.origin, status=excluded.status", (origin, task))
        conn.execute(f"DELETE FROM temp.{_TABLE}")
    except sqlite3.Error:
        # Missing migration/capture support must never claim a binding. No wire is
        # frozen without a non-NULL binding when it contains status.
        _log.warning("kanban status capture unavailable; retaining legacy envelopes", exc_info=True)


def capability(hub, board: str) -> dict:
    """Read the serving DO capability before each batch; do not cache over rollback."""
    from hermes_cli.kanban_sync_client import HubError
    method = getattr(hub, "status_origin_capability", None)
    if not callable(method):
        return {}
    try:
        cap = method(board)
    except HubError as exc:
        if exc.status not in (404, 405):
            raise
        return {}
    valid = (isinstance(cap, dict) and type(cap.get("status_origin")) is int and cap["status_origin"] == 1
             and type(cap.get("frozen_replay")) is int and cap["frozen_replay"] == 1
             and isinstance(cap.get("hlc"), str) and re.fullmatch(r"\d{13}-\d{4}-server", cap["hlc"]))
    return cap if valid and isinstance(cap, dict) else {}


def absorb_clock(conn: sqlite3.Connection, cap: dict) -> None:
    """Absorb serving HLC in the envelope transaction, before first-send stamps.

    Never clamp a remote clock: that would acknowledge losing first sends.
    Immutable retries keep their original HLC regardless of this max merge.
    """
    if not cap:
        return
    remote = cap["hlc"]
    last = conn.execute("SELECT hlc_last FROM kanban_sync_state").fetchone()
    if last and remote[:18] > str(last[0] or "")[:18]:
        conn.execute("UPDATE kanban_sync_state SET hlc_last=?", (remote,))


def add_origin(conn: sqlite3.Connection, row: dict, wire: dict) -> dict:
    """Attach persisted capture evidence only when the captured status still travels."""
    if row.get("table_name") == "tasks" and "status" in wire.get("fields", {}):
        current = conn.execute("SELECT sync_version FROM tasks WHERE id=?", (row["row_uid"],)).fetchone()
        if current and current[0] is None and row.get("op") == "upsert":
            # An unseen-task FULL CURRENT snapshot must remain internally
            # consistent. Bind its current status to that status's persisted
            # capture descriptor, never to this historical delta or latest event
            # observed at send. No envelope has been frozen yet on this path.
            saved = conn.execute("SELECT origin, status FROM kanban_sync_status_origins WHERE task_id=?",
                                 (row["row_uid"],)).fetchone()
            if saved and saved[0] is not None and saved[1] == wire["fields"]["status"]:
                wire["status_origin"] = saved[0]
            return wire
        if row.get("status_origin") is None:
            return wire
        captured = row.get("fields", {})
        captured = json.loads(captured) if isinstance(captured, str) else captured
        if "status" in captured:
            # Full-row enrichment may see a later local status. That status is a
            # different intent with its own outbox op, never this op's origin.
            wire["fields"]["status"] = captured["status"]
            wire["status_origin"] = row["status_origin"]
    return wire


def freezeable(row: dict, wire: dict) -> bool:
    """An unbound historical or synthesized status is not upgraded by guessing."""
    return row.get("table_name") != "tasks" or "status" not in wire.get("fields", {}) or "status_origin" in wire
