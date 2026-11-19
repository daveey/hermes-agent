"""Strict evidence payloads; no inferred provenance or user-authored content."""
from __future__ import annotations

import base64
import binascii
import hashlib
import math
import re
from typing import Mapping, NoReturn

CREATION_KEYS = frozenset((
    "task_uid", "creator_task_uid", "creator_episode_uid", "priority_intent", "priority_argument",
    "effective_parent_priority", "parent_snapshot_cut", "inheritance_cap", "stored_child_priority",
    "producer_source_hash", "unknown_fields",
))
MAX_METADATA_STRING = 1024


def _bad(message) -> NoReturn:
    from hermes_cli.kanban_fairness_schema import BadRecord

    raise BadRecord(message)


def _keys(payload, expected, name):
    if set(payload) != expected:
        _bad(f"{name} payload keys must be exactly {sorted(expected)}")


def _string(value, name, *, optional=False, bounded=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or (not value and not bounded):
        _bad(f"{name} must be a {'bounded' if bounded else 'nonempty'} string")
    if bounded and len(value.encode("utf-8")) > MAX_METADATA_STRING:
        _bad(f"{name} exceeds the string limit")


def _integer(value, name, *, optional=False, nonnegative=False):
    if value is None and optional:
        return
    if type(value) is not int or (nonnegative and value < 0):
        _bad(f"{name} must be {'a nonnegative' if nonnegative else 'an'} integer")


def creation_payload(payload):
    _keys(payload, CREATION_KEYS, "creation")
    _string(payload["task_uid"], "task_uid")
    for name in ("creator_task_uid", "creator_episode_uid"):
        _string(payload[name], name, optional=True)
    if payload["priority_intent"] not in ("explicit", "omitted", "unknown"):
        _bad("priority_intent must be explicit, omitted or unknown")
    if isinstance(payload["priority_argument"], (Mapping, list, tuple)):
        _bad("priority_argument must be a JSON scalar or null")
    for name in ("effective_parent_priority", "inheritance_cap"):
        _integer(payload[name], name, optional=True)
    _integer(payload["stored_child_priority"], "stored_child_priority")
    _string(payload["producer_source_hash"], "producer_source_hash", optional=True, bounded=True)
    cut = payload["parent_snapshot_cut"]
    if cut is not None:
        if not isinstance(cut, Mapping):
            _bad("parent_snapshot_cut must be an object or null")
        _keys(cut, {"source_gateway", "source_epoch", "seq"}, "parent_snapshot_cut")
        for name in ("source_gateway", "source_epoch"):
            _string(cut[name], f"parent_snapshot_cut.{name}")
        _integer(cut["seq"], "parent_snapshot_cut.seq", nonnegative=True)
    unknown = payload["unknown_fields"]
    if (not isinstance(unknown, list) or any(not isinstance(name, str) for name in unknown)
            or len(unknown) != len(set(unknown)) or not set(unknown) <= CREATION_KEYS):
        _bad("unknown_fields must be a unique list of known creation field names")
    if any(value is None and name != "priority_argument" and name not in unknown
           for name, value in payload.items()):
        _bad("null creation provenance must appear in unknown_fields")
    return dict(payload)


URGENCY_KEYS = frozenset((
    "receipt_type", "board", "read_at_s", "outcome", "raw_b64", "raw_sha256", "raw_complete",
    "observed_at", "valid_until", "source_revision", "owner_ids", "reason", "raw_size", "redacted",
))
URGENCY_OUTCOMES = ("valid", "missing", "invalid", "expired", "io_error", "oversize", "invalid_request")


def _finite(value, name):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _bad(f"{name} must be a finite number or null")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        _bad(f"{name} must be a finite number or null")


def _raw_receipt(payload):
    encoded, digest, size = (payload[name] for name in ("raw_b64", "raw_sha256", "raw_size"))
    _integer(size, "raw_size", optional=True, nonnegative=True)
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        _bad("raw_sha256 must be a SHA-256 hex digest or null")
    if payload["redacted"] and encoded is not None:
        _bad("redacted receipt must not include raw_b64")
    if encoded is None:
        return
    if not isinstance(encoded, str):
        _bad("raw_b64 must be base64 or null")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        _bad("raw_b64 must be valid base64")
    if hashlib.sha256(raw).hexdigest() != digest:
        _bad("raw_sha256 does not match raw_b64")
    if len(raw) != size:
        _bad("raw_size does not match raw_b64")


def urgency_payload(payload):
    _keys(payload, URGENCY_KEYS, "owner_generation")
    if payload["receipt_type"] != "urgency_read":
        _bad("receipt_type must be urgency_read")
    # Invalid requests still need to preserve the requested board, including an empty string.
    if not isinstance(payload["board"], str):
        _bad("board must be a string")
    if payload["outcome"] not in URGENCY_OUTCOMES:
        _bad("unknown urgency outcome")
    for name in ("read_at_s", "observed_at", "valid_until"):
        _finite(payload[name], name)
    _string(payload["source_revision"], "source_revision", optional=True, bounded=True)
    for name in ("raw_complete", "redacted"):
        if type(payload[name]) is not bool:
            _bad(f"{name} must be a boolean")
    owners = payload["owner_ids"]
    if (not isinstance(owners, list) or any(not isinstance(owner, str) or not owner for owner in owners)
            or owners != sorted(set(owners))):
        _bad("owner_ids must be a sorted unique list of nonempty strings")
    reason = payload["reason"]
    if reason is not None and (not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason)):
        _bad("reason must be a bounded fixed-code string or null")
    _raw_receipt(payload)
    if payload["outcome"] == "valid" and (
            not payload["raw_complete"] or payload["redacted"] or payload["raw_b64"] is None):
        _bad("valid urgency receipt must include complete unredacted bytes")
    if payload["outcome"] in ("missing", "io_error", "oversize", "invalid_request") and payload["raw_complete"]:
        _bad("unavailable urgency receipt cannot be raw_complete")
    if payload["outcome"] == "invalid" and not payload["redacted"]:
        _bad("invalid urgency bytes must be redacted")
    return dict(payload)
