"""Durable HOME intake/custody, independent of the worker operator ledger.

Rows are transport bookkeeping only. No stage grants release or execution.
Immutable carrier bytes and a once-frozen consume identity survive restarts.
"""
from __future__ import annotations

import json
import re

from hermes_cli import kanban_release_certificate as cert, kanban_release_intent as intent

TABLE = "owner_release_home_queue"
LIMIT = 262144
_FIELDS = {"uid", "task_id", "run_uid", "kind", "payload", "created_at", "hlc", "origin_gateway", "deleted_at"}
_PAYLOAD = {"version", "carrier", "action", "request_uid", "board", "target_task_id", "home", "intent", "digest", "seen_comments", "nonce"}


def carrier(value):
    return isinstance(value, dict) and (value.get("carrier") == "owner_release" or value.get("action") == "owner_release")


def validate(row, board, own):
    """Pure validation; the caller must independently authenticate the row."""
    if (not isinstance(row, dict) or not _FIELDS <= set(row) or row["deleted_at"] is not None
            or row["run_uid"] is not None or row["kind"] != "operator_call_requested"
            or not intent._coordinate(row["uid"]) or type(row["created_at"]) is not int
            or row["created_at"] < 0 or not isinstance(row["payload"], str) or len(row["payload"]) > LIMIT):
        raise ValueError("invalid HOME release carrier event")
    p = cert.parse(row["payload"])
    if not cert._exact(p, _PAYLOAD):
        raise ValueError("invalid HOME release carrier fields")
    body = intent.validate(p["intent"])
    if (type(p["version"]) is not int or p["version"] != 1 or p["carrier"] != "owner_release" or p["action"] != "owner_release"
            or p["request_uid"] != row["uid"] or p["board"] != board or body["board"] != board
            or p["target_task_id"] != row["task_id"] or body["task_id"] != row["task_id"]
            or p["home"] != own or body["home"] != own or body["actor"]["gateway"] == own
            or row["origin_gateway"] != body["actor"]["gateway"] or p["digest"] != intent.digest(body)
            or cert.encode(p["intent"]) != cert.encode(body) or row["uid"] == body["intent_uid"]
            or not isinstance(p["nonce"], str) or re.fullmatch(r"[a-f0-9]{64}", p["nonce"]) is None
            or not cert._comments(p["seen_comments"], body["task_id"])):
        raise ValueError("HOME release carrier identity differs")
    if (not isinstance(row["hlc"], str) or re.fullmatch(r"[0-9]{13}-[0-9]{4}-" + re.escape(body["actor"]["gateway"]), row["hlc"]) is None
            or len(json.dumps(p["seen_comments"], ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8", errors="backslashreplace")) > 131072):
        raise ValueError("HOME release carrier clock or inventory bound differs")
    return p


def install(conn):
    conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (delivery_uid TEXT PRIMARY KEY, board TEXT NOT NULL, "
                 "task_id TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL, intent_uid TEXT NOT NULL, "
                 "stage TEXT NOT NULL, consume_wire TEXT, wire_digest TEXT, checked INTEGER NOT NULL DEFAULT 0)")
    conn.execute("CREATE TABLE IF NOT EXISTS owner_release_home_scan (board TEXT PRIMARY KEY,last_id INTEGER NOT NULL,last_uid TEXT)")
    abort = "BEGIN SELECT RAISE(ABORT,'HOME release custody immutable'); END"
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_delete BEFORE DELETE ON {TABLE} {abort}")
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_replace BEFORE INSERT ON {TABLE} "
                 f"WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE delivery_uid=NEW.delivery_uid OR rowid=NEW.rowid) {abort}")
    conn.execute(f"CREATE TRIGGER IF NOT EXISTS {TABLE}_update BEFORE UPDATE ON {TABLE} WHEN "
                 "NEW.delivery_uid IS NOT OLD.delivery_uid OR NEW.board IS NOT OLD.board OR NEW.task_id IS NOT OLD.task_id "
                 "OR NEW.payload IS NOT OLD.payload OR NEW.created_at IS NOT OLD.created_at OR NEW.intent_uid IS NOT OLD.intent_uid "
                 "OR (OLD.consume_wire IS NOT NULL AND (NEW.consume_wire IS NOT OLD.consume_wire OR NEW.wire_digest IS NOT OLD.wire_digest)) "
                 "OR NOT (NEW.stage=OLD.stage OR (OLD.stage='observed' AND NEW.stage='prepared') "
                 "OR (OLD.stage='prepared' AND NEW.stage='consume_pending') "
                 "OR (OLD.stage='consume_pending' AND NEW.stage='consumed_pending_apply')) " + abort)
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_home_release_pending ON {TABLE}(board,stage,checked)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_home_release_intent ON {TABLE}(board,intent_uid)")


def exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone() is not None


def intake(conn, rep, board, own):
    """At most 16 bounded carrier payloads per tick, atomic with own UID cursor.

    Invalid shapes advance, while authenticated I/O retries rotate in the queue.
    Existing worker scan watermarks/refusals have no suppression authority here.
    """
    from hermes_cli import kanban_db as kb, kanban_operator_call as operator
    last = 0
    if exists(conn):
        saved = conn.execute("SELECT last_id,last_uid FROM owner_release_home_scan WHERE board=?", (board,)).fetchone()
        if saved and operator._uid_at(rep, saved[0]) == saved[1]:
            last = saved[0]
    rows = rep.execute("SELECT id,uid,task_id,created_at,payload FROM task_events WHERE id>? "
                       "AND kind='operator_call_requested' AND length(CAST(payload AS BLOB))<=? AND json_valid(payload) "
                       "AND (json_extract(payload,'$.carrier')='owner_release' OR json_extract(payload,'$.action')='owner_release') "
                       "ORDER BY id LIMIT 16", (last, LIMIT)).fetchall()
    if not rows:
        return
    with kb.write_txn(conn):
        install(conn)
        for eid, uid, task, created, raw in rows:
            try:
                p = cert.parse(raw)
                body = intent.validate(p.get("intent"))
            except (ValueError, TypeError):
                continue
            if p.get("home") != own or body["home"] != own or body["board"] != board:
                continue
            old = conn.execute(f"SELECT payload,task_id,created_at FROM {TABLE} WHERE delivery_uid=?", (uid,)).fetchone()
            if old is None:
                conn.execute(f"INSERT INTO {TABLE} (delivery_uid,board,task_id,payload,created_at,intent_uid,stage) "
                             "VALUES (?,?,?,?,?,?,'observed')", (uid, board, task, raw, created, body["intent_uid"]))
        conn.execute("INSERT INTO owner_release_home_scan VALUES (?,?,?) ON CONFLICT(board) DO UPDATE SET "
                     "last_id=excluded.last_id,last_uid=excluded.last_uid", (board, rows[-1][0], rows[-1][1]))


def wire(row):
    p = cert.parse(row["payload"])
    op = cert.parse(row["consume_wire"])
    expected = {"client_op_id": op.get("client_op_id"), "op": "consume_owner_release", "task": row["task_id"],
                "intent_uid": row["intent_uid"], "digest": p["digest"], "seen_comments": p["seen_comments"]}
    if (cert.encode(op) != row["consume_wire"] or intent.digest(op) != row["wire_digest"]
            or cert.encode(op) != cert.encode(expected) or not intent._coordinate(op["client_op_id"])
            or op["client_op_id"] in {row["delivery_uid"], row["intent_uid"]}):
        raise ValueError("HOME release consume journal changed")
    return op
