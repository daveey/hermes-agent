"""Atomic owner-side creation intents; publication/receipt transport runs later.

The request ID, card and immutable publication marker commit in the same board
transaction. No network I/O is allowed here. Ordinary (non-origin) creation keeps
its legacy idempotency semantics; receipt workflows use this stricter ledger.
"""
from __future__ import annotations

import json
import re
import secrets

from hermes_cli.kanban_origin import OriginError


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def create_with_origin(conn, intent: dict, options: dict) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_origin_ledger import validate_binding

    try:
        if not isinstance(intent, dict) or set(intent) != {"request_id", "owner", "origin"}:
            raise ValueError
        if not isinstance(intent["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", intent["request_id"]):
            raise ValueError
        if not isinstance(intent["owner"], dict) or set(intent["owner"]) != {"gateway", "profile", "board"}:
            raise ValueError
        owner = intent["owner"]
        if owner["board"] != (options["board"] or kb.get_current_board()):
            raise ValueError
        validate_binding({"binding_id": "validation", "version": 1, "publication_version": 1,
                          "owner": {**owner, "task_id": "validation"}, "origin": intent["origin"],
                          "task_record_key": f"{owner['profile']}/{owner['board']}/validation"})
    except (KeyError, TypeError, ValueError):
        raise OriginError("origin_creation_invalid", "invalid owner/origin creation intent") from None

    options = dict(options)
    options["parents"] = list(options["parents"])
    if options["skills"] is not None:
        options["skills"] = list(options["skills"])
    immutable = _json({"intent": intent, "options": options})
    with kb.write_txn(conn, allow_nested=True):
        conn.execute("""CREATE TABLE IF NOT EXISTS origin_creation_intents (
            request_id TEXT PRIMARY KEY NOT NULL,
            intent_json TEXT NOT NULL,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            binding_json TEXT NOT NULL,
            child_request_id TEXT NOT NULL UNIQUE)""")
        existing = conn.execute(
            "SELECT intent_json,task_id FROM origin_creation_intents WHERE request_id=?",
            (intent["request_id"],),
        ).fetchone()
        if existing:
            if existing["intent_json"] != immutable:
                raise OriginError("origin_creation_conflict", "request ID reused with a different creation intent")
            return existing["task_id"]
        # The receipt request ID, not legacy best-effort idempotency, owns this
        # creation. Never adopt an unrelated existing task by its legacy key.
        options["idempotency_key"] = None
        task_id = kb.create_task(conn, **options)
        marker = {"binding_id": "binding-" + secrets.token_hex(16), "version": 1,
                  "owner": {**intent["owner"], "task_id": task_id}, "origin": intent["origin"],
                  "task_record_key": f"{intent['owner']['profile']}/{intent['owner']['board']}/{task_id}"}
        conn.execute("INSERT INTO origin_creation_intents VALUES (?,?,?,?,?)",
                     (intent["request_id"], immutable, task_id, _json(marker), "origin-bind-" + secrets.token_hex(16)))
        return task_id


def adopt_with_origin(conn, intent: dict, *, board: str) -> str:
    """Attach a receipt intent to an exact existing card, never mutate its state.

    The trusted owner adapter checks the effective gateway/profile/database.
    This transaction verifies board/task identity and freezes the operation for
    retries. Publication and origin acknowledgement remain separate gates.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_origin_ledger import validate_binding

    try:
        if not isinstance(intent, dict) or set(intent) != {"request_id", "owner", "origin"}:
            raise ValueError
        if not isinstance(intent["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", intent["request_id"]):
            raise ValueError
        owner = intent["owner"]
        marker = {"binding_id": "validation", "version": 1,
                  "owner": owner, "origin": intent["origin"],
                  "task_record_key": f"{owner['profile']}/{owner['board']}/{owner['task_id']}"}
        validate_binding({**marker, "publication_version": 1})
        if owner["board"] != board:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise OriginError("origin_adoption_invalid", "invalid owner/origin adoption intent") from None

    immutable = _json({"operation": "adopt", "intent": intent})
    task_id = owner["task_id"]
    with kb.write_txn(conn, allow_nested=True):
        if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
            raise OriginError("origin_task_missing", "exact owner task does not exist")
        conn.execute("""CREATE TABLE IF NOT EXISTS origin_creation_intents (
            request_id TEXT PRIMARY KEY NOT NULL,
            intent_json TEXT NOT NULL,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            binding_json TEXT NOT NULL,
            child_request_id TEXT NOT NULL UNIQUE)""")
        existing = conn.execute(
            "SELECT intent_json,task_id FROM origin_creation_intents WHERE request_id=?",
            (intent["request_id"],),
        ).fetchone()
        if existing:
            if existing["intent_json"] != immutable:
                raise OriginError("origin_creation_conflict", "request ID reused with a different origin intent")
            return existing["task_id"]
        for row in conn.execute("SELECT request_id,binding_json FROM origin_creation_intents WHERE task_id=?", (task_id,)):
            try:
                previous = json.loads(row["binding_json"])
            except (TypeError, ValueError):
                raise OriginError("origin_adoption_conflict", "existing task binding is unreadable") from None
            if not isinstance(previous, dict):
                raise OriginError("origin_adoption_conflict", "existing task binding is unreadable")
            if previous.get("owner") == owner and previous.get("origin") == intent["origin"]:
                raise OriginError("origin_already_bound", "retry existing origin request " + row["request_id"])
        marker["binding_id"] = "binding-" + secrets.token_hex(16)
        conn.execute("INSERT INTO origin_creation_intents VALUES (?,?,?,?,?)",
                     (intent["request_id"], immutable, task_id, _json(marker), "origin-bind-" + secrets.token_hex(16)))
        return task_id
