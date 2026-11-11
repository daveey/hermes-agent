"""Bounded, additive artifact-disposition receipts; never execution authorization.

Stored inside existing outcome/run metadata, not a new ledger. Validation checks
claims' shape, not their truth: ``independently_checked`` is attributed evidence,
not permission to bypass acceptance, owner attestation or freshness gates.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any

DISPOSITIONS = ("delivered", "superseded", "contained", "rejected")
VERIFICATIONS = ("reported", "independently_checked")
MAX_ITEMS = 32
MAX_EVIDENCE = 32
MAX_REMAINING = 32
MAX_TEXT = 2000
MAX_REF = 2048
_QUALIFIED_OWNER = re.compile(r"@(task|session):[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+/[^\s/@]+\Z")


def _object(value: Any, required: set[str], optional: set[str] | None = None) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - (optional or set()):
        raise ValueError("completion_receipt: missing or unknown object fields")
    return value


def _text(value: Any, name: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"completion_receipt: {name} needs nonempty text of at most {limit} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"completion_receipt: {name} contains control characters")
    return value


def _enum(value: Any, choices: tuple[str, ...], name: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"completion_receipt: {name} must be one of {', '.join(choices)}")
    return value


def _array(value: Any, name: str, limit: int, *, nonempty: bool = False) -> list:
    if not isinstance(value, list) or len(value) > limit or (nonempty and not value):
        raise ValueError(f"completion_receipt: {name} needs {'1' if nonempty else '0'}..{limit} entries")
    return value


def _evidence(value: Any) -> dict:
    raw = _object(value, {"ref", "observed_at", "verification"})
    observed_at = _text(raw["observed_at"], "observed_at", 64)
    try:
        stamp = datetime.fromisoformat(observed_at)
    except ValueError as exc:
        raise ValueError("completion_receipt: observed_at needs an ISO8601 timestamp with timezone") from exc
    if stamp.tzinfo is None:
        raise ValueError("completion_receipt: observed_at needs a timezone")
    return {"ref": _text(raw["ref"], "evidence ref", MAX_REF), "observed_at": observed_at,
            "verification": _enum(raw["verification"], VERIFICATIONS, "verification")}


def _remaining(value: Any) -> dict:
    raw = _object(value, {"scope", "state", "owner", "reason"})
    state = _enum(raw["state"], ("pending", "deferred"), "remaining state")
    owner = raw["owner"]
    if owner is None:
        if state != "deferred":
            raise ValueError("completion_receipt: pending remaining work needs a qualified owner")
    elif not _QUALIFIED_OWNER.fullmatch(_text(owner, "owner", MAX_REF)):
        raise ValueError("completion_receipt: owner must be @task:board@gateway/id or @session:profile@gateway/id")
    return {"scope": _text(raw["scope"], "remaining scope"), "state": state, "owner": owner,
            "reason": _text(raw["reason"], "remaining reason")}


def _item(value: Any) -> dict:
    raw = _object(value, {"scope", "disposition", "evidence", "remaining"}, {"superseded_by"})
    disposition = _enum(raw["disposition"], DISPOSITIONS, "disposition")
    out = {"scope": _text(raw["scope"], "scope"), "disposition": disposition,
           "evidence": [_evidence(e) for e in _array(raw["evidence"], "evidence", MAX_EVIDENCE, nonempty=True)],
           "remaining": [_remaining(r) for r in _array(raw["remaining"], "remaining", MAX_REMAINING)]}
    if disposition == "superseded":
        out["superseded_by"] = _text(raw.get("superseded_by"), "superseded_by", MAX_REF)
    elif "superseded_by" in raw:
        raise ValueError("completion_receipt: superseded_by is only valid for superseded scope")
    return out


def validate_completion_receipt(value: Any) -> dict:
    """Validate the entire v1 value before any write; return an independent copy.

    This is structural validation only. Writers must additionally resolve pending
    owners against existing ownership/wait links before admitting the write.
    Projections preserve the source evidence timestamp and verification unchanged.
    """
    raw = _object(value, {"version", "items"})
    if type(raw["version"]) is not int or raw["version"] != 1:
        raise ValueError("completion_receipt: version must be integer 1")
    out = {"version": 1, "items": [_item(i) for i in _array(raw["items"], "items", MAX_ITEMS, nonempty=True)]}
    if len(json.dumps(out, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) > 32768:
        raise ValueError("completion_receipt: canonical JSON must not exceed 32768 bytes")
    return out
