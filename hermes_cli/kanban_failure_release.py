"""Durable run-scoped failure commands for authoritative primary Boards. [daveey]

Queue under the caller's write transaction BEFORE changing mirrored task/run rows.
A command acknowledgement is not a mirrored state: only the Board feed finalizes
those rows and emits the public failure notice.
"""
from __future__ import annotations

import json
import sqlite3

from hermes_cli import kanban_db_sync as sync


def deferred_crash(conn: sqlite3.Connection, dead) -> bool:
    """Charged primary crashes wait for authoritative release, not local mirror edits."""
    if sync.board_mode(conn) != "primary":
        return False
    if dead.host_reboot and dead.reboot_culprit:
        return True  # Culprit attribution outranks an owner loss during the same reboot.
    return not (
        dead.rate_limited or dead.owner_restart or dead.owner_unavailable
        or dead.host_reboot
    )


def route_dead_worker(conn: sqlite3.Connection, row, dead, sweep, pid: int) -> bool:
    """Route a classified dead worker before local closure; True when the Board owns it.

    Neutral releases wait for the feed; charged primary crashes join the sweep's
    accounting without a local mirror edit. Otherwise mark the canonical notice.
    """
    from hermes_cli import kanban_neutral_release
    if kanban_neutral_release.queue(conn, row, dead):
        return True
    if deferred_crash(conn, dead):
        sweep.crashed.append(row["id"])
        sweep.crash_details.append((row["id"], pid, row["claim_lock"], dead))
        return True
    if not (dead.rate_limited or dead.owner_restart
            or dead.owner_unavailable or (dead.host_reboot and not dead.reboot_culprit)):
        dead.event_payload["failure_notice"] = "canonical"
    return False


def _command(conn: sqlite3.Connection, run_id: int | None):
    """Read custody without migrating a legacy table from a read-only caller."""
    if sync.board_mode(conn) != "primary":
        return None
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_failure_commands'").fetchone():
        return None
    return conn.execute("SELECT * FROM kanban_failure_commands WHERE run_uid=?",
                        (sync.run_uid(conn, run_id),)).fetchone()


def command_owned(conn: sqlite3.Connection, run_id: int | None) -> bool:
    """Any first-wins command owns the run; observers must not race a retain."""
    return _command(conn, run_id) is not None


def queued(conn: sqlite3.Connection, run_id: int | None) -> bool:
    """Legacy release-only predicate: retain custody is never failure/quota authority."""
    row = _command(conn, run_id)
    return row is not None and (_is_release(row) or _fallback(row))


def _fallback(row) -> bool:
    return "rejected_release_op_id" in row.keys() and bool(row["rejected_release_op_id"])


def _is_release(row) -> bool:
    return "operation_kind" not in row.keys() or row["operation_kind"] in (None, "release")


def observer_held(conn: sqlite3.Connection, run_id: int | None) -> bool:
    """Rejected retain keeps immutable custody, but cannot disable failure enforcement.

    A fallback marker is permanent release custody, even after transport remaps/ACK.
    Unknown delivery is not a rejection and must not race a possibly applied retain.
    """
    row = _command(conn, run_id)
    return row is not None and (_is_release(row) or _fallback(row) or row["delivery_state"] != "rejected")


def release_predicate(conn: sqlite3.Connection) -> str:
    """SQL for existing joined readers (command alias c), including unmigrated NULL legacy rows."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(kanban_failure_commands)")}
    if "operation_kind" not in columns:
        return "1"
    fallback = " OR COALESCE(c.rejected_release_op_id,'')!=''" if "rejected_release_op_id" in columns else ""
    return f"(c.operation_kind IS NULL OR c.operation_kind='release'{fallback})"


def _ensure_commands(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_failure_commands ("
                 "run_uid TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
                 "op_id TEXT NOT NULL)")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(kanban_failure_commands)")}
    for name in ("operation_kind", "request_uid", "event_uid", "request_json", "request_sha256",
                 "delivery_state", "canonical_receipt_json", "rejected_release_op_id"):
        if name not in existing:
            conn.execute(f"ALTER TABLE kanban_failure_commands ADD COLUMN {name} TEXT")


def mark_sent(conn: sqlite3.Connection, run_uid: str, task_id: str) -> None:
    """Record that ``run_uid``'s release went to the Board synchronously (first wins: no later outbox release)."""
    _ensure_commands(conn)
    conn.execute("INSERT OR IGNORE INTO kanban_failure_commands (run_uid,task_id,op_id,operation_kind) "
                 "VALUES (?, ?, ?, 'release')", (run_uid, task_id, "sync"))


def queue(conn: sqlite3.Connection, task_id: str, run_id: int | None, *,
          outcome: str, error: str, **policy) -> bool:
    """True means Board authority owns this failure (including an already queued run).

    A missing portable identity on a primary board is an error, never permission
    to fall back to a competing local counter. Receipts survive outbox acks.
    """
    if sync.board_mode(conn) != "primary":
        return False
    uid = sync.run_uid(conn, run_id)
    if not uid:
        raise ValueError("primary failure release requires a portable run identity")
    row = conn.execute("SELECT task_id FROM task_runs WHERE id=?", (run_id,)).fetchone()
    if row[0] != task_id:
        raise ValueError("failure release run belongs to another task")
    if not conn.in_transaction:
        raise ValueError("failure release requires a write transaction")
    _ensure_commands(conn)
    op_id = sync.uuid7()
    inserted = conn.execute("INSERT OR IGNORE INTO kanban_failure_commands (run_uid,task_id,op_id,operation_kind) "
                            "VALUES (?, ?, ?, 'release')",
                            (uid, task_id, op_id)).rowcount
    if not inserted:
        # A definitive rejection made no canonical terminal mutation. Keep its
        # frozen audit intact and reserve one ordinary failure release atomically.
        # This marker is not the current outbox identity (retries may remap it).
        inserted = conn.execute("UPDATE kanban_failure_commands SET rejected_release_op_id=? "
                                "WHERE run_uid=? AND operation_kind='retain' AND delivery_state='rejected' "
                                "AND rejected_release_op_id IS NULL", (op_id, uid)).rowcount
    if inserted:
        fields = {**policy, "run_uid": uid, "outcome": outcome, "error": error[:500]}
        # Same millisecond clock as captured rows: a release may reference a
        # systemic fix card created earlier in this transaction.
        conn.execute("INSERT INTO kanban_sync_outbox "
                     "(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                     f"VALUES (?, 'tasks', ?, 'release', ?, '', {sync._NOW_MS_SQL})",
                     (op_id, task_id, json.dumps(fields)))
    return True
