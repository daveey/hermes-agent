"""Canonical Board certification of an owner reassessment receipt.

Pure verifier over an authenticated Board ``/evidence?table=task_events`` response
(``Hub.row_evidence``). Neither the sender's pending journal, a replica event, nor any
self-asserted JSON grants anything: the Board's own ``owner_reassessment_v1``
attestation must agree, field by field, with the canonical park event row it carries.
Unknown, missing or unavailable proof is a refusal. The receipt always has
``execution_authority`` false; this module installs no suppression or capability.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from hermes_cli.kanban_owner_reassessment import ReassessmentRefused, _uid, producer, request

MAX_EVIDENCE_BYTES = 256 * 1024
PARK_KINDS = ("scheduled", "continued")
_RECEIPT = {"version", "board", "task_id", "gateway", "episode_uid", "park_event", "issuer", "producer",
            "target", "issued_at", "reassess_at", "backstop_at", "evidence_refs", "suppresses",
            "execution_authority"}
_PARK = {"uid", "run_uid", "kind", "created_at", "payload"}
_ISSUER = {"owner_admitted_uid", "profile", "run_uid", "session_id", "worker_bound_uid"}
# Board re-stamps these on an unchanged row; they are not certified identity.
ROW_BOOKKEEPING = frozenset({"seq", "hlc", "updated_at", "version"})


def _int(v: Any) -> bool:
    return type(v) is int and v > 0


def _str(v: Any) -> bool:
    return isinstance(v, str) and bool(v)


def _no_dupes(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate JSON key")
        out[k] = v
    return out


def _refuse_const(name):
    raise ValueError(f"non-finite JSON constant {name}")


def parse_evidence(raw: str | bytes) -> Any:
    """Bounded parse of serialized evidence: size is checked *before* json parsing.

    Duplicate keys and NaN/Infinity are refused. This bounds stored/pickup text only; the Hub
    transport's own raw-response read is a separate seam (see ``Hub._call``).
    """
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_EVIDENCE_BYTES:  # chars <= bytes
        raise ReassessmentRefused("canonical reassessment evidence is too large or not text")
    if isinstance(raw, str) and len(raw.encode()) > MAX_EVIDENCE_BYTES:
        raise ReassessmentRefused("canonical reassessment evidence is too large")
    try:
        return json.loads(raw, object_pairs_hook=_no_dupes, parse_constant=_refuse_const)
    except (ValueError, RecursionError) as exc:
        raise ReassessmentRefused("canonical reassessment evidence is not valid JSON") from exc


def canonical_bytes(evidence: Any) -> str:
    """Serialized evidence, bounded; raises before any storage on oversize/non-finite."""
    try:
        raw = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReassessmentRefused("canonical reassessment evidence is not JSON") from exc
    if len(raw.encode()) > MAX_EVIDENCE_BYTES:
        raise ReassessmentRefused("canonical reassessment evidence is too large")
    return raw


def certified_receipt(evidence: Any, *, board: str, task: str, uid: str, sender: Mapping | None = None) -> dict:
    """Return the Board-attested receipt for park event ``uid`` or raise ReassessmentRefused.

    ``sender`` (optional) is the sending side's receipt; it must equal the Board's
    attestation exactly, so a sender can never widen what the Board attested.
    """
    canonical_bytes(evidence)
    bad = ReassessmentRefused
    if (not isinstance(evidence, Mapping) or evidence.get("board") != board or evidence.get("table") != "task_events"
            or evidence.get("uid") != uid or evidence.get("state") != "live" or not _uid(uid)):
        raise bad("canonical reassessment event identity is unproven")
    row, r = evidence.get("row"), evidence.get("owner_reassessment_v1")
    if not isinstance(row, Mapping) or not isinstance(r, Mapping) or set(r) != _RECEIPT:
        raise bad("canonical reassessment attestation is missing or malformed")
    park, issuer = r["park_event"], r["issuer"]
    if (not isinstance(park, Mapping) or set(park) != _PARK or not isinstance(issuer, Mapping)
            or set(issuer) != _ISSUER):
        raise bad("canonical reassessment attestation is malformed")
    try:
        payload = parse_evidence(park["payload"]) if isinstance(park["payload"], str) else None
    except ReassessmentRefused:
        payload = None
    try:  # exact target / deadline / evidence refs: the same validator the sender used
        request({"version": 1, "reassess_at": r["reassess_at"], "target": r["target"],
                 "evidence_refs": r["evidence_refs"]})
    except ReassessmentRefused as exc:
        raise bad("canonical reassessment target, deadline or evidence refs are invalid") from exc
    awaiting = payload.get("awaiting") if isinstance(payload, dict) else None
    ok = (
        type(r["version"]) is int and r["version"] == 1 and r["board"] == board and r["task_id"] == task
        and _str(r["gateway"]) and _uid(r["episode_uid"]) and r["episode_uid"] != uid
        and _str(issuer["profile"]) and _str(issuer["session_id"]) and r["execution_authority"] is False
        and r["suppresses"] == ["too_long"]
        and park["uid"] == uid and _uid(park["run_uid"]) and park["kind"] in PARK_KINDS
        and _int(park["created_at"]) and isinstance(park["payload"], str)
        and issuer["run_uid"] == park["run_uid"] and _uid(issuer["owner_admitted_uid"])
        and _uid(issuer["worker_bound_uid"])
        and all(_int(r[k]) for k in ("issued_at", "reassess_at", "backstop_at"))
        and r["issued_at"] < r["reassess_at"] <= r["backstop_at"]
        and isinstance(payload, dict) and payload.get("owner_reassessment_episode_uid") == r["episode_uid"]
        and payload.get("wake_at") == r["reassess_at"]
        and (park["kind"] == "scheduled" or payload.get("status") == "scheduled")
        and isinstance(awaiting, dict) and r["producer"] is not None and producer(awaiting) == r["producer"]
        and producer(r["producer"]) == r["producer"]
        # the canonical row, not the attestation, must carry the same bytes and identity
        and row.get("uid") == uid and row.get("task_id") == task and row.get("kind") == park["kind"]
        and row.get("payload") == park["payload"] and row.get("run_uid") == park["run_uid"]
        and row.get("created_at") == park["created_at"] and row.get("origin_gateway") == r["gateway"]
        and row.get("deleted_at") is None
    )
    if not ok:
        raise bad("canonical reassessment attestation does not match the canonical park event")
    if sender is not None and dict(sender) != dict(r):
        raise bad("sender receipt differs from the Board attestation")
    return deepcopy(dict(r))


def certified_identity(evidence: Mapping) -> str:
    """Certified fields only: the row without server bookkeeping, plus the attestation."""
    row = {k: v for k, v in evidence["row"].items() if k not in ROW_BOOKKEEPING}
    return json.dumps([row, evidence["owner_reassessment_v1"]], sort_keys=True, separators=(",", ":"))


def event_matches(event: Mapping, receipt: Mapping) -> bool:
    """Does a local/replica event row equal the attested park event?"""
    p = receipt["park_event"]
    return (event.get("uid") == p["uid"] and event.get("task_id") == receipt["task_id"]
            and event.get("kind") == p["kind"] and event.get("payload") == p["payload"]
            and event.get("created_at") == p["created_at"]
            and (event.get("origin_gateway") in (None, receipt["gateway"])))


def current_matches(receipt: Mapping, task: Mapping, *, latest_park_uid: str | None, now: int) -> bool:
    """Is *this* receipt still the live park? Historical receipts for the same card never match.

    ``latest_park_uid`` is the uid of the card's newest park event (see
    ``replica.latest_park_uid``); a later park/continue, however similar, supersedes the receipt.
    ``now`` must be before the receipt's own backstop. A task ``wake_at``, when supplied, must equal
    the attested ``reassess_at``.
    """
    return (task.get("id") == receipt["task_id"] and task.get("status") == "scheduled"
            and task.get("current_run_id") is None
            and latest_park_uid == receipt["park_event"]["uid"]
            and type(now) is int and now < receipt["backstop_at"]
            and ("wake_at" not in task or task["wake_at"] == receipt["reassess_at"]))
