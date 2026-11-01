"""Trusted producer completions for unrouted local (inline TUI/Desktop) sessions. [daveey]

A routed session (gateway route, local policy receipt, adopted legacy binding) takes a producer completion
through ``gateway.session_automation.admit_automation``. An old inline session has no route, and adopting it
needs an authenticated native transport and refuses a live inline owner
(``gateway.session_local_migration.adopt_legacy_session``). This module is that session's ingress: a durable
outbox in the session's own profile store, drained ONLY by the inline runtime that owns the chat, through the
existing notice channel (``tui_gateway.producer_admission``): the per-session poller and the cold-chat sweeper.

Contract (in-process core API; there is no RPC for it, so a viewer or remote client cannot reach it)::

    from hermes_state_producer_admission import admit_producer_completion, read_producer_admission
    receipt = admit_producer_completion(
        producer='["user-terminal","<job id>","final"]',   # the gateway's automation identity, verbatim
        profile_home=Path(home), stored_session_id=sid, text=notice)

``receipt['outcome']`` is one of:

- ``admitted``: durable. ``status`` is ``queued`` (waiting for the owner to be idle), ``committed`` (the notice
  row is in the transcript, its turn not yet started) or ``dispatched`` (its turn started).
- ``routed``: the session has a route (now, or since admission): admit through ``admit_automation`` with the
  same identity; nothing was written here (or the queued notice was retired as ``rerouted``, never delivered).
- ``missing``: ``reason`` is ``not_found`` (no such session in that store), ``wrong_profile`` (the row belongs to
  another profile), ``unknown_profile_home`` (``profile_home`` is not a Hermes profile with a store),
  ``not_local`` (a messaging session, which has its own authority) or ``session_deleted`` (gone after admission).
- ``retry``: storage busy or unavailable; retry with the same arguments.

Invalid arguments, an unregistered producer kind, or the same producer with a different payload raise
``ValueError``. Retrying an admission with the same arguments returns the stored receipt: one transcript row and
one turn per producer identity per profile store, across retries, races between backends and restarts.

Trust: the row is a ``process_complete`` notice (``role=user`` with a display kind, like every machine notice),
run as a synthesized turn: no ``turn_author``, no human-input stamp, no ``@`` reference expansion, and the
producer cannot choose its display kind. Core decides who may produce (``TRUSTED_PRODUCER_KINDS``).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

TRUSTED_PRODUCER_KINDS = frozenset({"user-terminal"})
DISPLAY_KIND = "process_complete"
MAX_TEXT_CHARS = 16000
MAX_PRODUCER_CHARS = 300

QUEUED, COMMITTED, DISPATCHED, REROUTED, MISSING = "queued", "committed", "dispatched", "rerouted", "missing"
PENDING = (QUEUED, COMMITTED)

_TABLE = """CREATE TABLE IF NOT EXISTS producer_admissions (
    producer TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    stored_session_id TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    message_row_id INTEGER,
    message_uid TEXT,
    delivered_session_id TEXT,
    created_at REAL NOT NULL,
    settled_at REAL)"""
_INDEX = "CREATE INDEX IF NOT EXISTS idx_producer_admissions_owner ON producer_admissions(owner, status)"


def validate_producer(producer: Any) -> str:
    """The producer identity: a compact JSON array of non-empty strings whose first element is a kind core
    trusts (the same string ``gateway.session_automation.producer_identity`` stores for a routed session)."""
    if not isinstance(producer, str) or not producer or len(producer) > MAX_PRODUCER_CHARS:
        raise ValueError(f"producer identity must be a non-empty string of at most {MAX_PRODUCER_CHARS} chars")
    try:
        parts = json.loads(producer)
    except ValueError:
        raise ValueError("producer identity must be a JSON array") from None
    if (not isinstance(parts, list) or len(parts) < 2
            or not all(isinstance(p, str) and p and p.isprintable() for p in parts)
            or json.dumps(parts, separators=(",", ":")) != producer):
        raise ValueError("producer identity must be a compact JSON array of non-empty printable strings")
    if parts[0] not in TRUSTED_PRODUCER_KINDS:
        raise ValueError(f"producer kind {parts[0]!r} is not a trusted core producer")
    return producer


def _validate_text(text: Any) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be non-empty")
    if len(text) > MAX_TEXT_CHARS or "\x00" in text:
        raise ValueError(f"text must be at most {MAX_TEXT_CHARS} chars and contain no NUL")
    return text


def payload_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _receipt(row) -> dict:
    status = row["status"]
    out: dict[str, Any] = {"producer": row["producer"], "owner": row["owner"],
                           "stored_session_id": row["stored_session_id"], "payload_digest": row["payload_digest"]}
    if status == REROUTED:
        return {"outcome": "routed", "reason": row["reason"] or "adopted", **out}
    if status == MISSING:
        return {"outcome": "missing", "reason": row["reason"] or "session_deleted", **out}
    out.update(outcome="admitted", status=status)
    if row["message_uid"]:
        out.update(message_uid=row["message_uid"], delivered_session_id=row["delivered_session_id"])
    return out


def _table_exists(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='producer_admissions'").fetchone() is not None


def _lineage(db, conn, session_id: str) -> tuple[str, str]:
    """``(owner, tip)``: the conversation's compression root (core's lease key, which skips reset forks) and its
    live tip, both read on ``conn``."""
    return db._session_turn_lease_key_on_conn(conn, session_id), db._compression_tip_on(conn, session_id)


def route_reason(db, conn, session_id: str) -> Optional[str]:
    """Why ``session_id`` is not an unrouted local session (any segment of its compression chain counts), or None.
    ``not_local``/``wrong_profile`` refuse admission; the others route it through ``admit_automation``."""
    from hermes_state_local import POLICY_PREFIX
    from hermes_state_local_migration import LEGACY_LOCAL_SOURCES, LEGACY_PREFIX
    from hermes_state_compression import _walk_compression_chain, _CHAIN_STEP_SQL
    owner = db._session_turn_lease_key_on_conn(conn, session_id)
    chain = _walk_compression_chain(owner, lambda cur: conn.execute(_CHAIN_STEP_SQL, (cur,)).fetchone())
    for sid in dict.fromkeys([owner, *chain, session_id]):
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            continue
        row = dict(row)
        if row.get("profile_name") not in (None, "", db._own_profile_name() or "default"):
            return "wrong_profile"
        if any(row.get(k) for k in ("session_key", "chat_id", "origin_json")):
            return "gateway_route"
        if row["source"] not in LEGACY_LOCAL_SOURCES:
            return "not_local"
        if conn.execute("SELECT 1 FROM state_meta WHERE key IN (?, ?)",
                        (POLICY_PREFIX + sid, LEGACY_PREFIX + sid)).fetchone():
            return "local_receipt"
    return None


_REFUSED = ("not_local", "wrong_profile")


def open_profile_db(profile_home):
    """``SessionDB`` for ``profile_home``'s own store, or None when it is not a Hermes profile home with a store.
    Never falls back to another store."""
    from hermes_state import SessionDB
    try:
        path = Path(profile_home).expanduser().resolve() / "state.db"
    except (TypeError, OSError):
        return None
    if not path.is_file():
        return None
    db = SessionDB(path)
    if db._own_profile_name() is None:
        db.close()
        return None
    return db


def _storage_retry(exc: BaseException) -> dict:
    return {"outcome": "retry", "reason": "storage_busy" if "locked" in str(exc) or "busy" in str(exc)
            else "storage_unavailable"}


def admit_producer_completion(*, producer: str, profile_home, stored_session_id: str, text: str) -> dict:
    """Durably admit one trusted producer completion into an unrouted local session (see module docstring)."""
    producer = validate_producer(producer)
    text = _validate_text(text)
    if not isinstance(stored_session_id, str) or not stored_session_id:
        raise ValueError("stored_session_id is required")
    digest = payload_digest(text)
    try:
        db = open_profile_db(profile_home)
    except (sqlite3.Error, OSError) as exc:
        return _storage_retry(exc)
    if db is None:
        return {"outcome": "missing", "reason": "unknown_profile_home"}
    try:
        def write(conn):
            conn.execute(_TABLE)
            conn.execute(_INDEX)
            prior = conn.execute("SELECT * FROM producer_admissions WHERE producer = ?", (producer,)).fetchone()
            if prior is not None:
                if prior["payload_digest"] != digest:
                    raise ValueError("producer identity already admitted with a different payload")
                return _settle_route(db, conn, prior)
            if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (stored_session_id,)).fetchone() is None:
                return {"outcome": "missing", "reason": "not_found"}
            reason = route_reason(db, conn, stored_session_id)
            if reason in _REFUSED:
                return {"outcome": "missing", "reason": reason}
            if reason is not None:
                return {"outcome": "routed", "reason": reason, "producer": producer}
            owner, _tip = _lineage(db, conn, stored_session_id)
            conn.execute("INSERT INTO producer_admissions (producer, owner, stored_session_id, payload_digest, text, "
                         "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (producer, owner, stored_session_id, digest, text, QUEUED, time.time()))
            return _receipt(conn.execute("SELECT * FROM producer_admissions WHERE producer = ?", (producer,)).fetchone())
        return db._execute_write(write)
    except ValueError:
        raise
    except sqlite3.Error as exc:
        return _storage_retry(exc)
    finally:
        db.close()


def _settle_route(db, conn, row) -> dict:
    """Receipt for an existing row; inside a writer txn a pending row whose session gained a route since (or
    vanished) is retired first, so the caller learns to re-admit through ``admit_automation``."""
    if row["status"] == QUEUED:
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (row["owner"],)).fetchone() is None:
            _settle(conn, row["producer"], MISSING, "session_deleted")
        elif (reason := route_reason(db, conn, row["owner"])) is not None:
            _settle(conn, row["producer"], MISSING if reason in _REFUSED else REROUTED, reason)
        row = conn.execute("SELECT * FROM producer_admissions WHERE producer = ?", (row["producer"],)).fetchone()
    return _receipt(row)


def _settle(conn, producer: str, status: str, reason: Optional[str] = None) -> None:
    conn.execute("UPDATE producer_admissions SET status = ?, reason = ?, settled_at = ? WHERE producer = ? "
                 "AND status = ?", (status, reason, time.time(), producer, QUEUED))


def read_producer_admission(*, producer: str, profile_home) -> dict:
    """The current receipt for ``producer`` in ``profile_home``'s store: the ``admit_producer_completion`` shape,
    or ``{'outcome': 'absent'}``. A queued notice whose session was adopted (routed) is retired here."""
    producer = validate_producer(producer)
    try:
        db = open_profile_db(profile_home)
    except (sqlite3.Error, OSError) as exc:
        return _storage_retry(exc)
    if db is None:
        return {"outcome": "missing", "reason": "unknown_profile_home"}
    try:
        def write(conn):
            if not _table_exists(conn):
                return {"outcome": "absent"}
            row = conn.execute("SELECT * FROM producer_admissions WHERE producer = ?", (producer,)).fetchone()
            return {"outcome": "absent"} if row is None else _settle_route(db, conn, row)
        return db._execute_write(write)
    except sqlite3.Error as exc:
        return _storage_retry(exc)
    finally:
        db.close()


# --- Delivery side: called only by the owning inline runtime (tui_gateway.producer_admission). ---

def pending_for_owner(db, owner: str) -> list[dict]:
    """Undelivered rows (oldest first) for the conversation rooted at ``owner``. Read-only."""
    with db._read_ctx() as conn:
        if not _table_exists(conn):
            return []
        rows = conn.execute("SELECT * FROM producer_admissions WHERE owner = ? AND status IN (?, ?) "
                            "ORDER BY created_at, producer", (owner, *PENDING)).fetchall()
    return [dict(r) for r in rows]


def pending_owners(db) -> list[str]:
    with db._read_ctx() as conn:
        if not _table_exists(conn):
            return []
        return [r[0] for r in conn.execute("SELECT DISTINCT owner FROM producer_admissions WHERE status IN (?, ?)",
                                           PENDING).fetchall()]


def owner_of(db, session_id: str) -> str:
    with db._read_ctx() as conn:
        return db._session_turn_lease_key_on_conn(conn, session_id)


def commit_delivery(db, producer: str, target_session_id: str, staged: dict) -> tuple[str, Optional[int]]:
    """Write the notice row for a queued ``producer`` into ``target_session_id`` and mark it committed, in ONE
    writer txn. ``staged`` is the stamped message dict (content, display kind/metadata, timestamp, uid) the turn
    adopts. Returns ``(verdict, row id)``: ``written`` (committed now, by this call), ``moved`` (the conversation's live tip is
    not ``target_session_id``: the caller's runtime is stale, nothing written), or the row's settled status
    (``committed``/``dispatched`` written earlier by another deliverer -> row id; ``rerouted``/``missing``).
    Raises SessionTurnLeaseLostError while another backend runs a turn in the conversation."""
    def write(conn):
        row = conn.execute("SELECT * FROM producer_admissions WHERE producer = ?", (producer,)).fetchone()
        if row is None:
            return "missing", None
        if row["status"] != QUEUED:
            return row["status"], row["message_row_id"]
        if conn.execute("SELECT 1 FROM sessions WHERE id = ?", (row["owner"],)).fetchone() is None:
            _settle(conn, producer, MISSING, "session_deleted")
            return MISSING, None
        if (reason := route_reason(db, conn, row["owner"])) is not None:
            status = MISSING if reason in _REFUSED else REROUTED
            _settle(conn, producer, status, reason)
            return status, None
        if db._compression_tip_on(conn, row["owner"]) != target_session_id:
            return "moved", None
        db._check_transcript_write_guards(conn, target_session_id, None, reject_active_turn_lease=True)
        ended = conn.execute("SELECT ended_at FROM sessions WHERE id = ?", (target_session_id,)).fetchone()
        if ended is not None and ended["ended_at"] is not None:
            db._reopen_session_conn(conn, target_session_id)
        db._insert_message_rows(conn, target_session_id, [staged])
        db._bump_session_counters(conn, target_session_id, 1, 0, unit=True)
        conn.execute("UPDATE producer_admissions SET status = ?, message_row_id = ?, message_uid = ?, "
                     "delivered_session_id = ?, settled_at = ? WHERE producer = ? AND status = ?",
                     (COMMITTED, staged["_row_id"], staged.get("message_uid"), target_session_id, time.time(),
                      producer, QUEUED))
        return "written", staged["_row_id"]
    return db._execute_write(write, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)


def mark_dispatched(db, producer: str) -> None:
    db._execute_write(lambda conn: conn.execute(
        "UPDATE producer_admissions SET status = ?, settled_at = ? WHERE producer = ? AND status = ?",
        (DISPATCHED, time.time(), producer, COMMITTED)))


def turn_followed(db, row: dict) -> bool:
    """True when anything was written after a committed notice row in its conversation: its turn already ran
    (a deliverer died between starting the turn and recording it). Re-dispatching would run it twice."""
    from hermes_state_sessions import _LINEAGE_CTE_SQL
    sid = row["delivered_session_id"]
    with db._read_ctx() as conn:
        return conn.execute(_LINEAGE_CTE_SQL + " SELECT 1 FROM messages WHERE session_id IN (SELECT id FROM lineage) "
                            "AND id > ? AND role = 'assistant' LIMIT 1",
                            (sid, sid, row["message_row_id"])).fetchone() is not None


def committed_message(db, row: dict) -> Optional[dict]:
    """The committed notice row as a message dict (for re-staging after a refused dispatch), or None."""
    with db._read_ctx() as conn:
        msg = conn.execute("SELECT * FROM messages WHERE id = ?", (row["message_row_id"],)).fetchone()
    return dict(msg) if msg is not None else None


def mark_missing(db, producer: str, reason: str = "session_deleted") -> None:
    db._execute_write(lambda conn: _settle(conn, producer, MISSING, reason))
