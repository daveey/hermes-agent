"""Native read-first challenge and trusted CLI emission bookkeeping.

A flushed CLI output is emission evidence, not proof a person/model read it and
not a sandbox against same-UID code. No consumed/applied authority is granted.
Only the captured parent may confirm its private in-memory display receipt.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import secrets

from hermes_cli import kanban_release_certificate as certificate, kanban_release_intent as intent


@dataclass(frozen=True)
class Confirmation:
    body_bytes: str
    nonce: str
    invocation: str
    rows_bytes: str


class DisplayResult(dict):
    """Parent-only object identity; JSON cannot manufacture its confirmation."""
    def __init__(self, value, confirmation):
        super().__init__(value)
        self.confirmation = confirmation


def invocation_uid():
    return secrets.token_hex(24)


def inventory(hub, body):
    """Read only the authenticated current-valid endpoint, never ordinary rows."""
    value = hub.owner_release_comments(body["board"], body["intent_uid"])
    if (not certificate._exact(value, {"version", "intent", "digest", "comments"})
            or type(value["version"]) is not int or value["version"] != 1
            or intent.validate(value["intent"]) != body or intent.encode(value["intent"]) != intent.encode(body)
            or value["digest"] != intent.digest(body) or not certificate._comments(value["comments"], body["task_id"])):
        raise ValueError("invalid private owner-release comments evidence")
    # Match JSON.stringify UTF-8 size (including escaped historical surrogates).
    import json
    wire = json.dumps(value["comments"], ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(wire.encode("utf-8", errors="backslashreplace")) > 131072:
        raise ValueError("owner-release comments inventory exceeds its byte bound")
    return certificate.encode(value["comments"])


def _row(conn, body):
    return conn.execute("SELECT rows_bytes,nonce,issued_invocation,displayed_invocation,acknowledged "
                        "FROM native_owner_release_reads WHERE intent_uid=?", (body["intent_uid"],)).fetchone()


def prepare(conn, hub, body, *, seen):
    """An ACK can use only an earlier invocation's confirmed exact inventory."""
    from hermes_cli import kanban_db as kb
    if seen is not None and (not isinstance(seen, str) or len(seen) != 64 or any(c not in "0123456789abcdef" for c in seen)):
        raise ValueError("--seen-release needs an exact emitted challenge nonce")
    intent.stage(conn, body)
    rows = inventory(hub, body)  # network strictly outside the writer
    invocation = invocation_uid()
    with kb.write_txn(conn):
        current, captured = intent._current(conn, body)
        if current[3] not in {"authorized_pending_home", "read_first_pending"}:
            raise ValueError("release is not awaiting native read acknowledgement")
        conn.execute("""CREATE TABLE IF NOT EXISTS native_owner_release_reads (
            intent_uid TEXT PRIMARY KEY, rows_bytes TEXT NOT NULL, nonce TEXT NOT NULL,
            issued_invocation TEXT NOT NULL, displayed_invocation TEXT, acknowledged INTEGER NOT NULL
        )""")
        row = _row(conn, body)
        if row is not None and row[0] == rows:
            nonce = row[1]
            accepted = seen == nonce and row[3] is not None and row[3] != invocation
        else:
            nonce, accepted = secrets.token_hex(32), False
            conn.execute("INSERT INTO native_owner_release_reads VALUES (?,?,?,?,NULL,0) "
                         "ON CONFLICT(intent_uid) DO UPDATE SET rows_bytes=excluded.rows_bytes,nonce=excluded.nonce,"
                         "issued_invocation=excluded.issued_invocation,displayed_invocation=NULL,acknowledged=0",
                         (body["intent_uid"], rows, nonce, invocation))
        if accepted:
            conn.execute("UPDATE native_owner_release_reads SET acknowledged=1 WHERE intent_uid=? AND nonce=? AND rows_bytes=?",
                         (body["intent_uid"], nonce, rows))
        else:
            conn.execute("UPDATE native_owner_release_reads SET issued_invocation=? WHERE intent_uid=? AND nonce=? AND rows_bytes=?",
                         (invocation, body["intent_uid"], nonce, rows))
        conn.execute("UPDATE native_owner_release_intents SET stage='read_first_pending' WHERE intent_uid=?", (body["intent_uid"],))
        if captured.validated() is None:
            raise ValueError("native release custody expired before read journal commit")
    result = {"ok": True, "task_id": body["task_id"], "intent_uid": body["intent_uid"],
              "release_stage": "acknowledged_pending_home" if accepted else "read_first_pending"}
    if accepted:
        return result
    result["release_read"] = {"nonce": nonce, "comments": json.loads(rows)}
    return DisplayResult(result, Confirmation(intent.encode(body), nonce, invocation, rows))


def confirm(conn, hub, confirmation):
    """Fresh custody + fresh private comments; old receipts cannot confirm new rows."""
    from hermes_cli import kanban_db as kb
    if type(confirmation) is not Confirmation:
        raise ValueError("native display confirmation is parent-private")
    body = intent.validate(certificate.parse(confirmation.body_bytes))
    intent.stage(conn, body)
    rows = inventory(hub, body)
    with kb.write_txn(conn):
        current, captured = intent._current(conn, body)
        row = _row(conn, body)
        if (current[3] != "read_first_pending" or row is None or row[1] != confirmation.nonce
                or row[2] != confirmation.invocation):
            raise ValueError("native release display receipt was superseded")
        if rows != confirmation.rows_bytes or row[0] != rows:
            raise ValueError("native release comments changed before display confirmation")
        conn.execute("UPDATE native_owner_release_reads SET displayed_invocation=? WHERE intent_uid=? "
                     "AND nonce=? AND issued_invocation=? AND rows_bytes=?",
                     (confirmation.invocation, body["intent_uid"], confirmation.nonce, confirmation.invocation, rows))
        if captured.validated() is None:
            raise ValueError("native release custody expired before display commit")
    return {"ok": True, "display_confirmed": True}
