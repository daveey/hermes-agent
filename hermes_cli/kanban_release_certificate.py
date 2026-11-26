"""Strict historical Board-private consumption proof; NOT application permission.

The authenticated private endpoint certifies the private mark. This parser cannot
create certification from an event, caller fields, or a consumed-state projection.
HOME must separately revalidate every live hold before atomic local application.
"""
from __future__ import annotations

import json

from hermes_cli import kanban_release_intent as intent

# Whole envelope: bounded comments (128 KiB), up to 4096 covered coordinates and
# renewal JSON escaping. This does not raise the Board's individual source caps.
MAX_BYTES = 2 * 1024 * 1024
_EVENT = {"uid", "ord", "task_id", "run_uid", "kind", "payload", "created_at", "seq", "version",
          "updated_at", "hlc", "origin_gateway", "deleted_at"}
_PAYLOAD = {"version", "episode", "covered_restriction_uids", "request_uid", "owner_release_intent_uid",
            "owner_release_digest", "actor_kind", "actor_gateway", "actor_profile", "actor_session",
            "authority_kind", "authority_ref", "reason"}
_COMMENT = {"uid", "task_id", "author", "body", "created_at", "seq", "version", "updated_at", "hlc",
            "origin_gateway", "deleted_at"}


def encode(value: dict) -> str:
    """Lossless local JSON, including JS-escaped lone surrogates in old comments."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _exact(value, fields):
    return isinstance(value, dict) and set(value) == fields


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 9007199254740991


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate certificate JSON key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("nonfinite certificate JSON value")


def parse(raw: object, *, max_bytes: int = MAX_BYTES) -> dict:
    """Bound before decode and reject duplicate keys; no writes or authority."""
    if not isinstance(raw, str) or len(raw) > max_bytes or len(raw.encode("utf-8")) > max_bytes:
        raise ValueError("invalid or oversized owner-release certificate")
    try:
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid owner-release certificate JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("owner-release certificate must be an object")
    return value


def _comments(rows, task):
    if not isinstance(rows, list) or len(rows) > 512:
        return False
    uids = []
    for row in rows:
        if (not _exact(row, _COMMENT) or row["task_id"] != task or not intent._coordinate(row["uid"])
                or row["deleted_at"] is not None or not all(isinstance(row[k], str) for k in ("author", "body", "hlc"))
                or not intent._coordinate(row["origin_gateway"])
                or not all(_integer(row[k], 1) for k in ("seq", "version"))
                or not all(_integer(row[k]) for k in ("created_at", "updated_at"))):
            return False
        uids.append(row["uid"])
    # Coordinates are ASCII, so this matches canonical SQLite ORDER BY uid.
    return uids == sorted(set(uids))


def _payload(event, body, digest, consume_uid):
    payload = parse(event["payload"])
    expected = {"version": 1, "episode": body["episode"], "request_uid": consume_uid,
                "owner_release_intent_uid": body["intent_uid"], "owner_release_digest": digest,
                "actor_kind": "agent", "actor_gateway": body["actor"]["gateway"],
                "actor_profile": body["actor"]["profile"], "actor_session": body["actor"]["session"],
                "authority_kind": "chat", "authority_ref": None, "reason": body["note"]}
    if (not _exact(payload, _PAYLOAD) or type(payload["version"]) is not int
            or any(payload[k] != value for k, value in expected.items())):
        raise ValueError("owner-release renewal payload differs from its intent")
    covered = payload["covered_restriction_uids"]
    if (not isinstance(covered, list) or not 1 <= len(covered) <= 4096
            or not all(intent._coordinate(uid) for uid in covered)
            or covered != sorted(set(covered)) or body["episode"] not in covered):
        raise ValueError("invalid owner-release covered restriction identities")
    return payload


def validate(value: dict, *, board: str, task: str, intent_uid: str) -> dict:
    """Validate exact private reply bindings, never infer private certification."""
    if not _exact(value, {"version", "intent", "digest", "consume", "renewal"}) or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("invalid owner-release consumption certificate shape")
    # Validate all JSON values and full response size, including nested comments.
    try:
        parse(encode(value))
    except (TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid owner-release certificate encoding") from exc
    body = intent.validate(value["intent"])
    if (body != value["intent"] or body["board"] != board or body["task_id"] != task or body["intent_uid"] != intent_uid
            or body["actor"]["gateway"] == body["home"] or value["digest"] != intent.digest(body)):
        raise ValueError("owner-release certificate intent identity mismatch")
    op, event = value["consume"], value["renewal"]
    if (not _exact(op, {"client_op_id", "op", "task", "intent_uid", "digest", "seen_comments"})
            or op["op"] != "consume_owner_release" or not intent._coordinate(op["client_op_id"])
            or op["client_op_id"] == intent_uid or op["task"] != task or op["intent_uid"] != intent_uid
            or op["digest"] != value["digest"] or not _comments(op["seen_comments"], task)):
        raise ValueError("owner-release consumption identity mismatch")
    if (not _exact(event, _EVENT) or event["uid"] != op["client_op_id"] or event["task_id"] != task
            or event["kind"] != "scope_renewed" or event["run_uid"] is not None or event["deleted_at"] is not None
            # HOME may have been renamed before consumption. Only the private
            # authenticated Board endpoint attests that alias qualification; freeze
            # its actual event origin and compare it exactly on local event reads.
            or not intent._coordinate(event["origin_gateway"]) or type(event["version"]) is not int or event["version"] != 1
            or not all(_integer(event[k], 1) for k in ("ord", "seq"))
            or not all(_integer(event[k]) for k in ("created_at", "updated_at"))
            or not isinstance(event["hlc"], str)):
        raise ValueError("owner-release canonical renewal identity mismatch")
    _payload(event, body, value["digest"], op["client_op_id"])
    return value


def coverage(receipt: dict) -> frozenset[str]:
    """Historical scope IDs only. Caller must NOT treat consumption as local release."""
    return frozenset(parse(receipt["renewal"]["payload"])["covered_restriction_uids"])


def event_matches(event: dict, receipt: dict) -> bool:
    """Match immutable logical event across local ID/envelope remapping."""
    frozen = receipt["renewal"]
    fields = ("uid", "task_id", "kind", "created_at", "origin_gateway")
    if not isinstance(event, dict):
        return False
    if event.get("run_uid") is not None or event.get("run_id") is not None or event.get("deleted_at") is not None:
        return False
    try:
        if encode({k: event.get(k) for k in fields}) != encode({k: frozen[k] for k in fields}):
            return False
        return encode(parse(event.get("payload"))) == encode(parse(frozen["payload"]))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return False
