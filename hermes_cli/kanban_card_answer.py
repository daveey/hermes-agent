"""Exact-ask owner transition for a verified remote Desktop decision. [daveey]

The courier authenticates the sender and reads the operation from its source
store. An envelope is routing input, never proof that the user answered. This
module deliberately has no tool or generic CLI entry point.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable

from hermes_cli import kanban_db as kb

FIELD = "card_answer"
LIFECYCLE = ("created", "blocked", "block_loop_detected", "unblocked", "archived", "completed")


class AnswerRefused(ValueError):
    """A stale, conflicting or unverified answer must leave the card untouched."""


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def snapshot(conn, task: str) -> dict | None:
    """Portable frozen ask; local integer event ids must never cross gateways.

    Synced events have UUID identities. Unsynced fixtures retain local ids, but
    cannot be delivered remotely without the adapter's independent owner check.
    """
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
    if row is None or row["status"] not in ("blocked", "triage"):
        return None
    try:
        ask = json.loads(row["human_request"] or "null")
    except (ValueError, TypeError):
        return None
    if not isinstance(ask, dict) or not ask.get("action"):
        return None
    marks = ",".join("?" for _ in LIFECYCLE)
    events = conn.execute(
        f"SELECT * FROM task_events WHERE task_id=? AND kind IN ({marks}) ORDER BY id",
        (task, *LIFECYCLE),
    ).fetchall()
    # Include the complete lifecycle identity set, not importer-local ordering.
    # A repeated identical block therefore has a distinct generation on replicas.
    identities = sorted(str(e["uid"]) if "uid" in e.keys() and e["uid"] else f"local:{e['id']}"
                        for e in events)
    return {"task": task, "status": row["status"], "ask": ask,
            "generation": digest({"lifecycle": identities, "ask": ask, "status": row["status"]})}


def _receipt(conn, task, operation_id, fingerprint):
    for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind IN ('unblocked','archived') ORDER BY id DESC",
        (task,),
    ):
        payload = json.loads(row[0] or "{}")
        receipt = payload.get(FIELD) if isinstance(payload, dict) else None
        if not isinstance(receipt, dict) or receipt.get("operationId") != operation_id:
            continue
        if receipt.get("fingerprint") != fingerprint:
            raise AnswerRefused("operation_conflict")
        return receipt
    return None


def apply(conn, request: dict, *, owner_gateway: str, owner_profile: str, board: str,
          verify_source: Callable[[dict], dict]) -> dict:
    """Apply only after independent source-operation and owner verification.

    ``verify_source`` is a trusted adapter callback, NOT an envelope field. It
    reads an immutable recorded Desktop operation from the authenticated sending
    gateway and returns its exact target/decision fields. Network evidence is
    acquired before taking the board lock; the exact ask is rechecked under it.
    The transition API owns its transaction and post-commit archive cleanup.
    """
    fields = {"ownerGateway", "ownerProfile", "board", "task", "generation", "choice", "operationId"}
    if not isinstance(request, dict) or set(request) != fields:
        raise AnswerRefused("invalid_answer")
    if any(not isinstance(request[k], str) or not request[k] for k in fields):
        raise AnswerRefused("invalid_answer")
    if (request["ownerGateway"] != owner_gateway or request["ownerProfile"] != owner_profile
            or request["board"] != board or len(request["operationId"]) > 200
            or len(request["choice"]) > 2000 or len(request["generation"]) != 64):
        raise AnswerRefused("invalid_answer")
    fingerprint = digest(request)
    old = _receipt(conn, request["task"], request["operationId"], fingerprint)
    if old:
        return old
    # Replays of committed decisions do not depend on a now-offline origin.
    # Fresh applications never accept caller-supplied proof or a bare boolean.
    proof = verify_source(dict(request))
    if not isinstance(proof, dict) or proof.get("answer") != request or proof.get("recorded") is not True:
        raise AnswerRefused("unverified_user_decision")
    origin = proof.get("gateway")
    if not isinstance(origin, str) or not origin:
        raise AnswerRefused("unverified_user_decision")
    current = snapshot(conn, request["task"])
    if current is None or current["generation"] != request["generation"]:
        raise AnswerRefused("stale_ask")
    options = current["ask"].get("choices") or [
        {"label": "Unblock", "value": "unblock"}, {"label": "Archive", "value": "archive"},
    ]
    choices = [ch for ch in options if isinstance(ch, dict) and ch.get("value") == request["choice"]]
    if len(choices) != 1:
        raise AnswerRefused("unknown_choice")
    archive = not current["ask"].get("choices") and request["choice"] == "archive"
    receipt = {**request, "originGateway": origin, "fingerprint": fingerprint,
               "destination": "kanban_ask", "executionStarted": False,
               "decisionProvenance": "recorded_desktop_operation"}
    note = "Choice recorded in Desktop (remote): " + encoded(choices[0]) + "\nOperation: " + request["operationId"]

    def fence(c):
        if _receipt(c, request["task"], request["operationId"], fingerprint):
            return "already_recorded"
        fresh = snapshot(c, request["task"])
        return None if fresh == current else "stale_ask"

    transition = kb.archive_task if archive else kb.unblock_task
    transition(conn, request["task"], precondition=fence, comment=("user-steps", note),
               event_extra={FIELD: receipt})
    recorded = _receipt(conn, request["task"], request["operationId"], fingerprint)
    if not recorded:
        raise AnswerRefused("ask_changed_or_held")
    return recorded
