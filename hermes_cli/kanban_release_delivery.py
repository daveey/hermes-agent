"""Native read-ACK carrier custody. Publication never means HOME application.

One outstanding delivery per application; distinct immutable row-op and event IDs
survive reply loss. An ordinary mutable event is a carrier, not an authority proof.
HOME must authenticate it and the private intent before consuming or applying.
"""
from __future__ import annotations

import json
import time

from hermes_cli import kanban_release_certificate as certificate, kanban_release_intent as intent

_STAGES = {"prepared", "send_pending", "published"}
_WIRE = {"client_op_id", "op", "table", "uid", "fields", "hlc", "frozen_replay"}
_PAYLOAD = {"version", "carrier", "action", "request_uid", "board", "target_task_id", "home",
            "intent", "digest", "seen_comments", "nonce"}
_SCHEMA = """CREATE TABLE IF NOT EXISTS native_owner_release_deliveries (
    delivery_uid TEXT PRIMARY KEY, intent_uid TEXT NOT NULL, wire TEXT NOT NULL,
    digest TEXT NOT NULL, stage TEXT NOT NULL);
"""


def _saved(conn, body):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_deliveries'").fetchone():
        return None
    rows = conn.execute("SELECT delivery_uid,wire,digest,stage FROM native_owner_release_deliveries "
                        "WHERE intent_uid=? LIMIT 2",
                        (body["intent_uid"],)).fetchall()
    if len(rows) > 1:
        raise ValueError("ambiguous native release delivery")
    return rows[0] if rows else None


def _validate(wire, body):
    if (not certificate._exact(wire, _WIRE) or wire["op"] != "upsert" or wire["table"] != "task_events"
            or type(wire["frozen_replay"]) is not int or wire["frozen_replay"] != 1
            or not all(intent._coordinate(wire[k]) for k in ("client_op_id", "uid"))
            or len({wire["client_op_id"], wire["uid"], body["intent_uid"]}) != 3):
        raise ValueError("invalid frozen release delivery wire")
    fields = wire["fields"]
    if (not certificate._exact(fields, {"task_id", "run_uid", "kind", "payload", "created_at"})
            or fields["task_id"] != body["task_id"] or fields["run_uid"] is not None
            or fields["kind"] != "operator_call_requested" or type(fields["created_at"]) is not int
            or fields["created_at"] < 0 or not isinstance(wire["hlc"], str)):
        raise ValueError("invalid frozen release delivery event")
    import re
    if re.fullmatch(r"[0-9]{13}-[0-9]{4}-" + re.escape(body["actor"]["gateway"]), wire["hlc"]) is None:
        raise ValueError("invalid frozen release delivery clock")
    p = certificate.parse(fields["payload"])
    if (not certificate._exact(p, _PAYLOAD) or type(p["version"]) is not int or p["version"] != 1
            or p["carrier"] != "owner_release" or p["action"] != "owner_release"
            or p["request_uid"] != wire["uid"] or p["board"] != body["board"]
            or p["target_task_id"] != body["task_id"] or p["home"] != body["home"]
            or certificate.encode(p["intent"]) != certificate.encode(body) or p["digest"] != intent.digest(body)
            or not isinstance(p["nonce"], str) or re.fullmatch(r"[a-f0-9]{64}", p["nonce"]) is None
            or not certificate._comments(p["seen_comments"], body["task_id"])):
        raise ValueError("invalid frozen release delivery payload")
    # The string payload adds another JSON-escaping layer. Bound the FINAL op,
    # not only its source rows, against Board's 256-Ki-character operation limit.
    if len(certificate.encode(wire)) > 262144:
        raise ValueError("release delivery exceeds canonical operation bound")
    return wire


def existing(conn, body):
    stage = intent.stage(conn, body)  # fresh native actor, exact primary and original bytes
    row = _saved(conn, body)
    if row is None:
        if stage == "delivery_pending":
            raise ValueError("native release delivery journal missing")
        return None
    wire = _validate(certificate.parse(row[1]), body)
    if (row[0] != wire["uid"] or row[1] != certificate.encode(wire)
            or row[2] != intent.digest(wire) or row[3] not in _STAGES):
        raise ValueError("native release delivery journal changed")
    return wire


def prepare(conn, body, *, nonce):
    """Freeze the accepted read under one writer, before the first network call."""
    from hermes_cli import kanban_db as kb, kanban_sync_client as client
    from hermes_cli.kanban_db_sync import uuid7
    from hermes_cli.kanban_release_read import _row
    with kb.write_txn(conn):
        current, captured = intent._current(conn, body)
        old = existing(conn, body)
        if old is not None:
            return old  # never reinterpret an uncertain delivery using new rows
        row = _row(conn, body)
        if (current[3] != "read_first_pending" or row is None or row[1] != nonce
                or row[3] is None or row[4] != 1):
            raise ValueError("release delivery requires its exact acknowledged emitted inventory")
        comments = json.loads(row[0])
        if not certificate._comments(comments, body["task_id"]):
            raise ValueError("invalid acknowledged release comment inventory")
        uid, op_uid = uuid7(), uuid7()
        payload = {"version": 1, "carrier": "owner_release", "action": "owner_release", "request_uid": uid,
                   "board": body["board"], "target_task_id": body["task_id"], "home": body["home"],
                   "intent": body, "digest": intent.digest(body), "seen_comments": comments, "nonce": nonce}
        wire = _validate({"client_op_id": op_uid, "op": "upsert", "table": "task_events", "uid": uid,
                          "hlc": client._next_hlc(conn, body["actor"]["gateway"]), "frozen_replay": 1,
                          "fields": {"task_id": body["task_id"], "run_uid": None, "kind": "operator_call_requested",
                                     "payload": certificate.encode(payload), "created_at": int(time.time())}}, body)
        conn.execute(_SCHEMA)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_native_release_delivery_active "
                     "ON native_owner_release_deliveries(intent_uid) WHERE stage IN ('prepared','send_pending','published')")
        conn.execute("INSERT INTO native_owner_release_deliveries VALUES (?,?,?,?,?)",
                     (uid, body["intent_uid"], certificate.encode(wire), intent.digest(wire), "prepared"))
        conn.execute("UPDATE native_owner_release_intents SET stage='delivery_pending' WHERE intent_uid=?",
                     (body["intent_uid"],))
        if captured.validated() is None:
            raise ValueError("native release custody expired before delivery commit")
    return wire


def _advance(conn, body, wire, stage):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        current, captured = intent._current(conn, body)
        if current[3] != "delivery_pending":
            raise ValueError("native release delivery intent is no longer pending")
        if existing(conn, body) != wire:
            raise ValueError("native release delivery changed before publication")
        conn.execute("UPDATE native_owner_release_deliveries SET stage=? WHERE delivery_uid=? AND stage!='published'",
                     (stage, wire["uid"]))
        if captured.validated() is None:
            raise ValueError("native release custody expired before publication commit")


def _evidence(hub, body, wire):
    value = hub.row_evidence(body["board"], "task_events", wire["uid"], max_response_bytes=2097152)
    if (not isinstance(value, dict) or value.get("board") != body["board"]
            or value.get("table") != "task_events" or value.get("uid") != wire["uid"]):
        raise ValueError("release publication evidence coordinates differ")
    row = value.get("row")
    if value.get("state") == "live" and isinstance(row, dict):
        expected = {**wire["fields"], "uid": wire["uid"], "hlc": wire["hlc"],
                    "origin_gateway": body["actor"]["gateway"], "deleted_at": None}
        if all(k in row and certificate.encode(row[k]) == certificate.encode(v) for k, v in expected.items()):
            return "live"
    if (value.get("state") == "never_existed" and row is None and value.get("tombstone") is None
            and type(value.get("floor")) is int and value["floor"] == 0):
        return "never_existed"
    raise ValueError("release publication unavailable, deleted, unknown or conflicting")


def send(conn, hub, body):
    wire = existing(conn, body)
    if wire is None:
        raise ValueError("no frozen native release delivery")
    stage = intent.stage(conn, body)
    if stage != "delivery_pending":
        if stage not in {"expired", "refused", "consumed", "applied"}:
            raise ValueError("native release delivery intent is not pending")
        return {"ok": stage not in {"expired", "refused"}, "task_id": body["task_id"], "intent_uid": body["intent_uid"],
                "release_stage": {"applied": "consumed_or_applied", "consumed": "acknowledged_pending_home"}.get(stage, stage)}
    evidence = _evidence(hub, body, wire)  # no writer held over HTTP
    if evidence == "never_existed":
        row = _saved(conn, body)
        if row is None or row[3] == "published":
            raise ValueError("confirmed release publication disappeared")
        _advance(conn, body, wire, "send_pending")
        intent.stage(conn, body)  # reauthenticate immediately before network
        hub.ops(body["board"], [wire])
        if _evidence(hub, body, wire) != "live":
            raise ValueError("release publication not confirmed")
    _advance(conn, body, wire, "published")
    return {"ok": True, "task_id": body["task_id"], "intent_uid": body["intent_uid"],
            "release_stage": "acknowledged_pending_home", "delivery_uid": wire["uid"], "publication_stage": "published"}
