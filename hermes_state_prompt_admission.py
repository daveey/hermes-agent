"""Durable exact-session prompt receipts for the owning TUI runtime.

Acceptance and its user row commit together. Only the transaction winner may
start a turn; replay NEVER schedules execution, including after a process crash.
This is at-most-once dispatch, not an exactly-once execution promise.
"""
from __future__ import annotations

import hashlib
import json

_TABLE = """CREATE TABLE IF NOT EXISTS prompt_admissions (
    operation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    user_row_id INTEGER NOT NULL,
    message_uid TEXT NOT NULL,
    dispatch TEXT NOT NULL DEFAULT 'unknown'
)"""


def prompt_digest(session_id: str, text: str) -> str:
    payload = json.dumps({"stored_session_id": session_id, "text": text},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _receipt(conn, session_id, operation_id, payload_digest):
    row = conn.execute("SELECT * FROM prompt_admissions WHERE operation_id = ?", (operation_id,)).fetchone()
    if row is None:
        return None
    if row["session_id"] != session_id or row["payload_digest"] != payload_digest:
        raise ValueError("prompt admission identity conflict")
    # Row content can legitimately expand @files after admission. Identity, not
    # current content or active state, proves the original committed occurrence.
    message = conn.execute("SELECT role, message_uid FROM messages WHERE message_uid = ? AND session_id = ?",
                           (row["message_uid"], session_id)).fetchone()
    if message is None or message["role"] != "user" or message["message_uid"] != row["message_uid"]:
        raise ValueError("accepted prompt row is no longer verifiable; never replay this operation")
    dispatch = row["dispatch"] if "dispatch" in row.keys() else "unknown"
    if dispatch == 'cancelled' and conn.execute(
            "SELECT 1 FROM messages WHERE session_id = ? AND message_uid = ? AND active = 1",
            (session_id, row['message_uid'])).fetchone():
        raise ValueError("cancelled prompt row is active; cancellation is not verifiable")
    return {"status": "accepted", "stored_session_id": session_id, "operation_id": operation_id,
            "payload_digest": payload_digest, "user_row_id": row["user_row_id"],
            "message_uid": row["message_uid"], "execution_status": "unknown",
            "dispatch": row["dispatch"] if "dispatch" in row.keys() else "unknown"}


def read_admission(db, session_id: str, operation_id: str, payload_digest: str):
    with db._read_ctx() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_admissions'").fetchone():
            return None
        return _receipt(conn, session_id, operation_id, payload_digest)


def admit_prompt(db, session_id: str, operation_id: str, text: str, payload_digest: str):
    """Caller must own the runtime's writer lease and already claim the running turn.

    Returns (receipt, staged user dict, created). The dict is adopted by the normal
    turn's submit-row path. No cold resume, title lookup, or compression-tip lookup.
    """
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.message_metadata import stamp_message_timestamp, stamp_message_uid

    if not operation_id or not session_id or not isinstance(text, str) or not text.strip():
        raise ValueError("nonempty exact session, operation and text required")
    if prompt_digest(session_id, text) != payload_digest:
        raise ValueError("payload digest mismatch")
    staged = stamp_message_timestamp({"role": "user", "content": text})
    uid = stamp_message_uid(staged)

    def write(conn):
        conn.execute(_TABLE)
        if "dispatch" not in {r["name"] for r in conn.execute("PRAGMA table_info(prompt_admissions)")}:
            conn.execute("ALTER TABLE prompt_admissions ADD COLUMN dispatch TEXT NOT NULL DEFAULT 'unknown'")
        prior = _receipt(conn, session_id, operation_id, payload_digest)
        if prior is not None:
            return prior, None, False
        row = conn.execute("SELECT ended_at, end_reason FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None or row["end_reason"] == "compression":
            raise ValueError("exact session missing or rotated")
        db._check_transcript_write_guards(conn, session_id, None, reject_active_turn_lease=True)
        if row["ended_at"] is not None:
            db._reopen_session_conn(conn, session_id)
        db._insert_message_rows(conn, session_id, [staged])
        db._bump_session_counters(conn, session_id, 1, 0, unit=True)
        conn.execute("INSERT INTO prompt_admissions (operation_id, session_id, payload_digest, user_row_id, message_uid) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (operation_id, session_id, payload_digest, staged["_row_id"], uid))
        return _receipt(conn, session_id, operation_id, payload_digest), staged, True

    receipt, message, created = db._execute_write(write, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)
    if message is not None:
        message[_DB_PERSISTED_MARKER] = True
        message["_submit_row_session_id"] = session_id
    return receipt, message, created


def mark_dispatch_failed(db, session_id, operation_id, payload_digest):
    """Atomically retire a positively unstarted prompt, preserving its audit row.

    Caller must fence the scheduler and retain the writer claim until commit.
    Unknown execution outcomes MUST NOT call this function.
    """
    def write(conn):
        receipt = _receipt(conn, session_id, operation_id, payload_digest)
        if receipt is None:
            raise ValueError("cannot cancel an absent admission")
        db._check_transcript_write_guards(conn, session_id, None, reject_active_turn_lease=True)
        conn.execute("UPDATE messages SET active = 0, compacted = 0 "
                     "WHERE session_id = ? AND message_uid = ? AND role = 'user'",
                     (session_id, receipt["message_uid"]))
        conn.execute("UPDATE prompt_admissions SET dispatch = 'cancelled' WHERE operation_id = ?", (operation_id,))
        return _receipt(conn, session_id, operation_id, payload_digest)
    return db._execute_write(write, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)
