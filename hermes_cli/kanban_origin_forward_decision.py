"""Freeze origin-local authorization before the courier can enqueue a create.

The server-stamped input is supplied by a trusted local courier adapter. A retry
reuses the exact durable envelope; a fresh decision needs live execution custody.
No network writes, task creation, or completion claims happen in this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from hermes_cli.kanban_origin_forward import authorize

_COMPONENT = r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}"


def _matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _canonical(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("origin_forward_request_invalid") from exc
    if len(raw.encode("utf-8")) > 262144:
        raise ValueError("origin_forward_request_invalid")
    return raw


def _validate_create(create):
    from hermes_cli.kanban_creation_contract import validate_forward
    validate_forward(create)
    if (not all(isinstance(create.get(key), str) and create[key].strip()
                for key in ("title", "assignee", "session_id"))
            or not _matches(_COMPONENT, create["session_id"])):
        raise ValueError("origin_forward_create_invalid")


def _validated(request, gateway, profile):
    if not isinstance(request, dict) or not isinstance(request.get("args"), dict):
        raise ValueError("origin_forward_request_invalid")
    args = request["args"]
    origin = args.get("origin")
    destination = args.get("destination")
    if (not isinstance(origin, dict) or set(origin) != {"gateway", "profile", "session"}
            or not all(_matches(_COMPONENT, value) for value in origin.values())
            or origin["gateway"] != gateway or origin["profile"] != profile
            or request.get("to_gw") != gateway or request.get("coll") != "sessions"
            or request.get("action") != "origin-forward"
            or request.get("key") != f"{profile}/{origin['session']}"
            or not _matches("gw:" + _COMPONENT, request.get("from"))
            or not _matches(r"[A-Za-z0-9._:-]{8,128}", request.get("id"))):
        raise ValueError("origin_forward_route_invalid")
    if (not isinstance(destination, dict) or set(destination) != {"gateway", "profile", "board"}
            or not all(_matches(_COMPONENT, value) for value in destination.values())
            or not isinstance(args.get("creator"), dict) or not isinstance(args.get("create"), dict)
            or not isinstance(args.get("run_uid"), str) or not args["run_uid"]):
        raise ValueError("origin_forward_request_invalid")
    # Transport timestamps/claim counters are not part of the immutable request.
    return {key: request[key] for key in ("id", "from", "to_gw", "coll", "key", "action", "args")}


def _existing(db, identity, canonical):
    row = db.execute("SELECT request_json,envelope_json FROM decisions WHERE identity=?", (identity,)).fetchone()
    if row is None:
        return None
    if row[0] != canonical:
        raise ValueError("origin_forward_conflict")
    return json.loads(row[1])


def prepare(request: dict, *, path: Path, ledger: Path, gateway: str, profile: str,
            lease_lookup, inventory_lookup, now_ms, session_exists, native_lookup=None) -> dict:
    canonical = _canonical(_validated(request, gateway, profile))
    # Work only with a detached JSON snapshot, including across network I/O.
    request = json.loads(canonical)
    args = request["args"]
    identity = _canonical([gateway, profile, request["from"], request["id"]])
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS decisions (identity TEXT PRIMARY KEY, "
                   "request_json TEXT NOT NULL, envelope_json TEXT NOT NULL)")
        replay = _existing(db, identity, canonical)
        if replay is not None:
            return replay
        _validate_create(args["create"])
        creator = args["creator"]
        from hermes_cli.kanban_origin_ledger import validate_binding
        validate_binding({"binding_id": "validation", "version": 1, "publication_version": 1,
                          "origin": args["origin"], "owner": creator,
                          "task_record_key": "/".join(str(creator.get(k, ""))
                                                     for k in ("profile", "board", "task_id"))})
        lease = lease_lookup(creator["board"], creator["task_id"])
        authorize(ledger=ledger, origin=args["origin"], creator=creator, gateway=gateway,
                  profile=profile, sender=request["from"], run_uid=args["run_uid"],
                  lease=lease, now_ms=now_ms(), session_exists=session_exists, native_lookup=native_lookup)
        destination = args["destination"]
        from hermes_cli.kanban_origin_destination import require_destination
        # Membership is admission evidence only, never origin authority. A fresh
        # authenticated read follows authorization; durable replay skips it.
        require_destination(inventory_lookup(), gateway=gateway, destination=destination["gateway"])
        envelope = {"id": "origin-forward-" + hashlib.sha256(identity.encode()).hexdigest(),
                    "to_gw": destination["gateway"], "coll": "tasks", "action": "create",
                    "key": f"{destination['profile']}/{destination['board']}",
                    "args": {"origin": args["origin"], "create": args["create"]}}
        # Concurrent admissions may have raced the network read. Recheck while
        # holding write custody, and return only after durable commit.
        with db:
            db.execute("BEGIN IMMEDIATE")
            replay = _existing(db, identity, canonical)
            if replay is not None:
                return replay
            # Evidence reads and write-lock waits can outlive the authorized
            # lease. Only a previously durable decision may replay after expiry.
            if lease["expires"] <= now_ms():
                raise ValueError("origin_forward_lease_invalid")
            db.execute("INSERT INTO decisions VALUES (?,?,?)", (identity, canonical, _canonical(envelope)))
        return envelope
