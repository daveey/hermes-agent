"""Durable origin obligations and readback receipts, independent of the hub replica.

This module owns persistence, not transport. Callers supply the authenticated
courier sender and the origin gateway/profile from local configuration. Request
IDs are globally unique in that courier (its create API rejects envelope reuse).
Binding IDs are task-owner-qualified; each binding is an immutable revision,
not an update command. A version change requires a new binding ID. Observations,
verified release, lineage refresh and integration with projections are separate
lifecycle operations, not implied by an accepted binding receipt.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_binding(binding: dict) -> None:
    """Reject incomplete/ambiguous identities before writing an acknowledgement."""
    try:
        if set(binding) != {"binding_id", "version", "owner", "origin", "task_record_key", "publication_version"}:
            raise ValueError
        for field in ("version", "publication_version"):
            if type(binding[field]) is not int or binding[field] < 1:
                raise ValueError
        for field, keys in (("owner", {"gateway", "profile", "board", "task_id"}),
                            ("origin", {"gateway", "profile", "session"})):
            if set(binding[field]) != keys:
                raise ValueError
            for value in binding[field].values():
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", value):
                    raise ValueError
        if not isinstance(binding["binding_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", binding["binding_id"]):
            raise ValueError
        owner = binding["owner"]
        if binding["task_record_key"] != f"{owner['profile']}/{owner['board']}/{owner['task_id']}":
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("origin_binding_invalid") from None


def binding_hash(binding: dict) -> str:
    validate_binding(binding)
    return hashlib.sha256(_json(binding).encode()).hexdigest()


@contextmanager
def _db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS origin_obligations (
            binding_id TEXT PRIMARY KEY, binding_hash TEXT NOT NULL,
            binding_json TEXT NOT NULL, resolved_session TEXT NOT NULL,
            task_json TEXT NOT NULL, observation_version INTEGER NOT NULL,
            released INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS origin_receipts (
            request_id TEXT PRIMARY KEY NOT NULL, binding_hash TEXT NOT NULL,
            receipt_json TEXT NOT NULL)""")
        conn.commit()
        yield conn
    finally:
        conn.close()


def _binding_key(binding: dict) -> str:
    # IDs belong to the task owner, not to a global unqualified namespace.
    return _json([binding["owner"], binding["binding_id"]])


def _read_receipt(conn: sqlite3.Connection, request_id: str) -> dict | None:
    row = conn.execute("SELECT receipt_json FROM origin_receipts WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        return None
    ack = json.loads(row[0])
    obligation = conn.execute("SELECT binding_hash FROM origin_obligations WHERE binding_id=?", (_binding_key(ack),)).fetchone()
    if not obligation or obligation[0] != ack["binding_hash"]:
        raise ValueError("origin_obligation_missing")
    return ack


def receipt(path: Path, request_id: str) -> dict | None:
    with _db(path) as conn:
        return _read_receipt(conn, request_id)


def obligations(path: Path, *, gateway: str, profile: str, sessions: list[str]) -> list[dict]:
    result = []
    with _db(path) as conn:
        for raw, resolved, task, version, released in conn.execute(
                "SELECT binding_json,resolved_session,task_json,observation_version,released FROM origin_obligations"):
            binding = json.loads(raw)
            origin = binding["origin"]
            if (origin["gateway"] == gateway and origin["profile"] == profile
                    and (origin["session"] in sessions or resolved in sessions)):
                result.append({**binding, "resolved_session": resolved, "task": json.loads(task),
                               "observation_version": version, "unfinished": not bool(released)})
    return result


def accept_binding(*, path: Path, replica: Path, binding: dict, request_id: str,
                   sender: str, gateway: str, profile: str,
                   resolve_session: Callable[[str], str]) -> dict:
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", request_id):
        raise ValueError("origin_request_invalid")
    digest = binding_hash(binding)
    if (sender != f"gw:{binding['owner']['gateway']}"
            or gateway != binding['origin']['gateway'] or profile != binding['origin']['profile']):
        raise ValueError("origin_route_mismatch")
    prior = receipt(path, request_id)
    if prior:
        if prior["binding_hash"] != digest:
            raise ValueError("origin_binding_conflict")
        return prior
    if not replica.is_file():
        return {"status": "pending", "code": "origin_publication_pending"}
    from hermes_cli.kanban_origin import _ro
    db = _ro(replica)
    try:
        row = db.execute("SELECT version,data FROM replica WHERE gw=? AND coll='tasks' AND key=?",
                         (binding["owner"]["gateway"], binding["task_record_key"])).fetchone()
    finally:
        db.close()
    if not row or row[0] < binding["publication_version"]:
        return {"status": "pending", "code": "origin_publication_pending"}
    task = json.loads(row[1])
    owner = binding["owner"]
    if any(task.get(field) != owner[field] for field in ("gateway", "profile", "board")) or task.get("id") != owner["task_id"]:
        raise ValueError("origin_publication_mismatch")
    # The immutable marker is published BEFORE the server assigns a record
    # version. Only the courier envelope adds that publication lower bound.
    marker = {k: v for k, v in binding.items() if k != "publication_version"}
    markers = [{k: v for k, v in m.items() if k != "publication_version"}
               for m in task.get("origin_bindings", []) if isinstance(m, dict)]
    if marker not in markers:
        return {"status": "pending", "code": "origin_publication_pending"}
    resolved = resolve_session(binding["origin"]["session"])
    if not isinstance(resolved, str) or not resolved:
        raise ValueError("origin_session_missing")
    ack = {"status": "accepted", "request_id": request_id, "binding_id": binding["binding_id"],
           "binding_hash": digest, "origin": binding["origin"], "owner": binding["owner"],
           "resolved_session": resolved, "publication_version": row[0]}
    with _db(path) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _read_receipt(conn, request_id)
        if existing:
            if existing["binding_hash"] != digest:
                raise ValueError("origin_binding_conflict")
            return existing
        existing = conn.execute("SELECT binding_hash,resolved_session,observation_version FROM origin_obligations WHERE binding_id=?", (_binding_key(binding),)).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("origin_binding_conflict")
            # An immutable binding retry acknowledges what is actually durable,
            # not a newer resolution that this transaction did not store.
            ack["resolved_session"], ack["publication_version"] = existing[1], existing[2]
        conn.execute("INSERT INTO origin_obligations (binding_id,binding_hash,binding_json,resolved_session,task_json,observation_version) VALUES (?,?,?,?,?,?) ON CONFLICT(binding_id) DO NOTHING",
                     (_binding_key(binding), digest, _json(binding), resolved, _json(task), row[0]))
        conn.execute("INSERT INTO origin_receipts VALUES (?,?,?)", (request_id, digest, _json(ack)))
    readback = receipt(path, request_id)
    assert readback is not None
    return readback
