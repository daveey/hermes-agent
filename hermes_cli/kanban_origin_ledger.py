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
        if not isinstance(binding, dict) or set(binding) != {"binding_id", "version", "owner", "origin", "task_record_key", "publication_version"}:
            raise ValueError
        for field in ("version", "publication_version"):
            if type(binding[field]) is not int or binding[field] < 1:
                raise ValueError
        for field, keys in (("owner", {"gateway", "profile", "board", "task_id"}),
                            ("origin", {"gateway", "profile", "session"})):
            if not isinstance(binding[field], dict) or set(binding[field]) != keys:
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


def refresh_observations(path: Path, replica: Path) -> None:
    """Advance durable observations only from newer, exactly bound owner records.

    Disappearance is not completion. Corrupt/unreadable stores raise to let callers
    display unknown, rather than returning an empty successful projection. A newer
    nonterminal publication reopens a previously released obligation.
    """
    if not path.is_file() or not replica.is_file():
        return
    from hermes_cli.kanban_origin import _ro

    source = _ro(replica)
    try:
        with _db(path) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            for key, raw, observed in conn.execute(
                    "SELECT binding_id,binding_json,observation_version FROM origin_obligations").fetchall():
                binding = json.loads(raw)
                validate_binding(binding)
                row = source.execute(
                    "SELECT version,data FROM replica WHERE gw=? AND coll='tasks' AND key=?",
                    (binding['owner']['gateway'], binding['task_record_key'])).fetchone()
                if not row or type(row[0]) is not int or row[0] <= observed:
                    continue
                task = json.loads(row[1])
                owner = binding['owner']
                if (not isinstance(task, dict)
                        or any(task.get(f) != owner[f] for f in ('gateway', 'profile', 'board'))
                        or task.get('id') != owner['task_id']):
                    continue
                marker = {k: v for k, v in binding.items() if k != 'publication_version'}
                markers = task.get('origin_bindings')
                if not isinstance(markers, list) or marker not in [
                        {k: v for k, v in m.items() if k != 'publication_version'}
                        for m in markers if isinstance(m, dict)]:
                    continue
                released = task.get('status') in ('done', 'archived')
                conn.execute(
                    "UPDATE origin_obligations SET task_json=?,observation_version=?,released=? WHERE binding_id=?",
                    (_json(task), row[0], int(released), key))
    finally:
        source.close()


def project(path: Path, *, replica: Path | None, gateway: str, profile: str,
            now: float | None = None) -> list[dict]:
    """Owner-qualified receipt rows, including explicit unknown and released rows.

    Consumers filter this single projection, never infer release from an absent
    replica row. Old and resolved session aliases remain available for lineage
    joins. Replica failures are stale observations; ledger failures propagate.
    """
    import time
    from hermes_cli.kanban_origin import _ro

    if not path.exists():
        return []
    now = time.time() if now is None else now
    source = None
    fresh = False
    last_ok = None
    try:
        if replica is not None and replica.is_file():
            refresh_observations(path, replica)
            source = _ro(replica)
            meta = dict(source.execute("SELECT k,v FROM meta"))
            last_ok = float(meta.get('last_ok') or 0)
            fresh = (0 <= now - last_ok <= 180 and not meta.get('offline_since'))
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # A failed refresh never erases a receipt. Reading the ledger below
        # still raises if the authoritative store itself is broken.
        fresh = False
    result = []
    try:
        with _db(path) as conn:
            for raw, resolved, payload, version, released in conn.execute(
                    "SELECT binding_json,resolved_session,task_json,observation_version,released FROM origin_obligations"):
                binding = json.loads(raw)
                validate_binding(binding)
                origin, owner = binding['origin'], binding['owner']
                if (origin['gateway'], origin['profile']) != (gateway, profile):
                    continue
                task = json.loads(payload)
                current = False
                if fresh and source is not None:
                    row = source.execute(
                        "SELECT version,data FROM replica WHERE gw=? AND coll='tasks' AND key=?",
                        (owner['gateway'], binding['task_record_key'])).fetchone()
                    current = bool(row and row[0] == version and json.loads(row[1]) == task)
                status = task.get('status')
                # Acceptance alone is not a release, including an initially
                # terminal publication. Unknown status also remains unfinished.
                known = status in ('todo', 'ready', 'running', 'scheduled', 'review',
                                   'blocked', 'triage', 'done', 'archived')
                if not current or not known or (status in ('done', 'archived') and not released):
                    status = 'unknown'
                result.append({
                    **{k: task.get(k) for k in ('title', 'assignee', 'block_kind', 'created_at', 'started_at', 'priority')},
                    **owner, 'id': owner['task_id'],
                    'key': '/'.join(owner[k] for k in ('gateway', 'profile', 'board', 'task_id')),
                    'session': resolved, 'origin_session': origin['session'],
                    'origin_profile': origin['profile'], 'origin_gateway': origin['gateway'],
                    'binding_id': binding['binding_id'], 'observation_version': version,
                    'status': status, 'reported_status': task.get('status'),
                    'unfinished': not (current and released and status in ('done', 'archived')),
                    'stale': not current, 'synced_at': last_ok,
                    'remote': True, 'via': 'origin',
                })
        return result
    finally:
        if source is not None:
            source.close()


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
