"""Canonical Board containment receipts and replay-safe single-operation delivery.

No replica row, gateway label or request-supplied digest grants release authority.
The Board attests the exact event bytes after checking its private episode ledger.
This adapter is staged; it does not install local fences or advertise capability.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_containment_policy import identity
from hermes_cli.kanban_containment_state import _payload, _release, _stop, _uid, fingerprint


class DeliveryUnknown(RuntimeError):
    """The original request must be recovered, never replaced with another UID."""

    code = "containment_delivery_unknown"

    def __init__(self, message: str, *, request_uid: str | None = None, episode: str | None = None):
        super().__init__(message)
        self.request_uid, self.episode = request_uid, episode


def canonical_event(evidence: Any, *, board: str, uid: str, task: str, kind: str) -> dict:
    """Validate the authenticated /evidence response, not a shape-only replica.

    The caller obtains this value directly from its enrolled Hub connection.
    This function is deliberately not an authentication boundary against code
    possessing that connection/key. A gateway key holder is trusted ingress.
    """
    if (not isinstance(evidence, Mapping) or evidence.get("board") != board
            or evidence.get("table") != "task_events" or evidence.get("uid") != uid
            or evidence.get("state") != "live" or not _uid(uid)):
        raise ContainmentRefused("canonical containment event identity is unproven")
    row, proof = evidence.get("row"), evidence.get("containment_v1")
    if (not isinstance(row, Mapping) or row.get("uid") != uid or row.get("task_id") != task
            or row.get("kind") != kind or row.get("deleted_at") is not None
            or not isinstance(row.get("payload"), str) or not isinstance(proof, Mapping)
            or type(proof.get("version")) is not int or proof.get("version") != 1
            or not _uid(proof.get("episode")) or not _uid(proof.get("request"))
            or not isinstance(proof.get("gateway"), str) or not proof["gateway"]
            or row.get("origin_gateway") != proof["gateway"]):
        raise ContainmentRefused("canonical containment attestation is missing or inconsistent")
    return dict(row)


def certified_stop(evidence: Any, *, board: str, task: str, uid: str) -> dict:
    row = canonical_event(evidence, board=board, task=task, uid=uid, kind="contained")
    stop = _stop(row)
    proof = evidence["containment_v1"]
    if (stop is None or stop["target"]["board"] != board or proof["episode"] != uid
            or proof.get("containment_sha256") != fingerprint(stop)):
        raise ContainmentRefused("canonical frozen containment predicate is inconsistent")
    return row


def certified_release(stop_evidence: Any, release_evidence: Any, *, board: str, task: str,
                      episode: str, receipt: str) -> tuple[dict, dict]:
    stop = certified_stop(stop_evidence, board=board, task=task, uid=episode)
    release = canonical_event(release_evidence, board=board, task=task, uid=receipt,
                              kind="containment_released")
    proof = release_evidence["containment_v1"]
    if (proof["episode"] != episode or proof.get("containment_sha256") != fingerprint(_stop(stop))
            or not _release(release, stop)):
        raise ContainmentRefused("canonical release is not bound to this exact containment episode")
    return stop, release


def _schema(conn: sqlite3.Connection) -> None:
    # Host-local request custody, deliberately not in SYNCED_COLUMNS. No token or
    # free-text handoff is stored in this transport ledger.
    conn.execute("CREATE TABLE IF NOT EXISTS containment_delivery ("
                 "request_uid TEXT PRIMARY KEY, board TEXT NOT NULL, gateway TEXT NOT NULL, "
                 "op_json TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0)")


def stage(conn: sqlite3.Connection, *, board: str, gateway: str, op: dict) -> str:
    """Commit the exact operation BEFORE network I/O. Retrying cannot change it."""
    from hermes_cli import kanban_db as kb
    request = op.get("client_op_id")
    if (not _uid(request) or not _uid(op.get("event_uid"))
            or op.get("op") not in ("contain", "resume_contained") or not gateway):
        raise ContainmentRefused("containment delivery requires portable request/event identities")
    target = identity({"board": board, "gateway": gateway, "task_id": op.get("task")})
    if not _uid(op.get("actor_run_uid")) or not isinstance(op.get("payload"), dict):
        raise ContainmentRefused("containment delivery lacks actor or predicate")
    identity({**target, "task_id": op.get("actor_task")})
    raw = json.dumps(op, sort_keys=True, separators=(",", ":"), allow_nan=False)
    _schema(conn)
    with kb.write_txn(conn):
        prior = conn.execute("SELECT board,gateway,op_json FROM containment_delivery WHERE request_uid=?",
                             (request,)).fetchone()
        if prior and tuple(prior) != (board, gateway, raw):
            raise ContainmentRefused("request identity already binds different containment bytes")
        if not prior:
            conn.execute("INSERT INTO containment_delivery(request_uid,board,gateway,op_json) VALUES (?,?,?,?)",
                         (request, board, gateway, raw))
    return request


def deliver(conn: sqlite3.Connection, hub: Any, *, request: str) -> dict:
    """Send/recover one durable request; success requires exact canonical readback.

    A timeout, malformed response or missing attestation is UNKNOWN, never a
    license to create a replacement request. Board replay uses the same client
    operation identity. No local task/history/edge mutation occurs here.
    """
    if conn.in_transaction:
        raise ContainmentRefused("containment network delivery cannot run inside a writer transaction")
    _schema(conn)
    row = conn.execute("SELECT board,gateway,op_json,confirmed FROM containment_delivery WHERE request_uid=?",
                       (request,)).fetchone()
    if row is None:
        raise ContainmentRefused("unknown durable containment request")
    board, gateway, raw, confirmed = row
    if hub.ident.gateway != gateway:
        raise ContainmentRefused("containment request belongs to a different enrolled gateway")
    op = json.loads(raw)
    if confirmed < 0:
        raise ContainmentRefused("Board previously refused this exact containment request")
    if not confirmed:
        response = hub.ops(board, [op])
        results = response.get("results") if isinstance(response, dict) else None
        if (not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict)
                or results[0].get("client_op_id") != request):
            raise DeliveryUnknown("Board reply did not identify the exact containment request; recover it")
        verdict = results[0]
        if verdict.get("status") == "rejected":
            from hermes_cli import kanban_db as kb
            with kb.write_txn(conn):
                conn.execute("UPDATE containment_delivery SET confirmed=-1 WHERE request_uid=? AND op_json=?",
                             (request, raw))
            raise ContainmentRefused("Board refused containment: " + str(verdict.get("reason") or "unknown"))
        if verdict.get("status") not in ("applied", "merged"):
            raise DeliveryUnknown("Board containment disposition is unknown; recover the same request")
    event = hub.row_evidence(board, "task_events", op["event_uid"])
    if op["op"] == "contain":
        receipt = certified_stop(event, board=board, task=op["task"], uid=op["event_uid"])
    else:
        episode = op["payload"].get("episode")
        stop = hub.row_evidence(board, "task_events", episode)
        _, receipt = certified_release(stop, event, board=board, task=op["task"], episode=episode,
                                       receipt=op["event_uid"])
    if _payload(receipt) != op["payload"] or event["containment_v1"]["request"] != request:
        raise DeliveryUnknown("canonical receipt does not match the staged request bytes")
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        conn.execute("UPDATE containment_delivery SET confirmed=1 WHERE request_uid=? AND op_json=?",
                     (request, raw))
    return {"task_id": op["task"], "request_uid": request, "event_uid": op["event_uid"],
            "canonical_confirmed": True}
