"""Record shapes, thresholds and exit codes of the queue-fairness evidence capture. [daveey]

One capture is a journal of ``tasks`` row versions and lifecycle events taken from one Kanban database
(normally a board's full replica, so one host sees every gateway's cards), drained to hashed JSONL segments
and closed by a sealed ``manifest.json``. The reader (:mod:`kanban_fairness_metrics`) accepts only records
that decode here; anything else is a coverage gap, never a silently skipped line.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

VERSION = 1

# Acceptance gates (seconds), from t_01a1d774f7486668: stored-priority-0 median ready wait, the longest ready
# wait while claims happen, and the ready wait of alarm and land-turn owner cards.
P0_MEDIAN_S = 1800
MAX_WAIT_S = 7200
URGENT_WAIT_S = 600

PASS, FAIL, INDETERMINATE = "PASS", "FAIL", "INDETERMINATE"
EXIT = {PASS: 0, FAIL: 1, INDETERMINATE: 2}
EXIT_ERROR = 3

# Journal bounds: past either, the triggers count the change and store nothing (a recorded gap).
MAX_RECORDS = 100_000
MAX_BYTES = 64 * 1024 * 1024
SEGMENT_BYTES = 8 * 1024 * 1024

# ``tasks`` columns a row-version record may carry; absent columns (an unsynced board has no lease) are omitted.
TASK_FIELDS = ("status", "priority", "idempotency_key", "assignee", "ready_since", "lease_holder",
               "created_at", "sync_version")
# Lifecycle event kinds worth their journal row: who entered or left the queue and when the origin said so.
EVENT_KINDS = ("created", "promoted", "reclaimed", "unblocked", "claimed", "spawned", "completed", "blocked",
               "archived", "scheduled", "continued", "continuation_woken", "condition_met")

KINDS = ("opening", "task", "event")
TASK_OPS = ("insert", "update", "delete")
OWNER_SOURCES = ("core-lane", "state-turn")


class BadRecord(ValueError):
    """A line that is not a version-1 capture record."""


def _int(value: Any, name: str, *, optional: bool = False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRecord(f"{name} must be an integer")
    return value


ENVELOPE_KEYS = ("version", "capture_id", "source_gateway", "board_uid", "source_epoch", "seq", "kind",
                 "wall_ns", "mono_ns", "clock_error_ns", "tx_uid", "tick_uid", "stage_index", "payload")
# Phase A kinds only: ``stage`` and ``candidate_fact`` belong to Phase B option A and are rejected.
RECORD_KINDS = ("opening", "row_change", "owner_generation", "membership", "creation", "loss")
LIFECYCLE_KINDS = ("opening", "row_change")
LIFECYCLE_KEYS = ("task_id", "op", "fields", "journal_seq", "journal_kind", "database_uid", "source_uid")
MAX_PAYLOAD_BYTES = 256 * 1024
_MAX_DEPTH = 16


def _str(value: Any, name: str, *, optional: bool = False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise BadRecord(f"{name} must be a nonempty string" + (" or null" if optional else ""))
    return value


def _uint(value: Any, name: str, *, optional: bool = False, positive: bool = False):
    value = _int(value, name, optional=optional)
    if value is not None and value < (1 if positive else 0):
        raise BadRecord(f"{name} must be {'positive' if positive else 'nonnegative'}")
    return value


def _json_safe(value: Any, name: str, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise BadRecord(f"{name} is nested too deeply")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise BadRecord(f"{name} holds a non-finite number")
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _json_safe(item, name, depth + 1)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise BadRecord(f"{name} has a non-string key")
            _json_safe(item, name, depth + 1)
        return
    raise BadRecord(f"{name} holds a non-JSON value")


def _bounded_payload(payload: Any) -> dict:
    if not isinstance(payload, Mapping):
        raise BadRecord("payload must be an object")
    _json_safe(payload, "payload")
    if len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise BadRecord("payload exceeds the size limit")
    return dict(payload)


def _check_fields(journal_kind: str, op: Any, fields: Mapping) -> None:
    """The legacy flat-record semantics for ``fields``: task rows need status, events need kind/created_at."""
    if journal_kind == "task" and op not in TASK_OPS:
        raise BadRecord(f"unknown task op {op!r}")
    if journal_kind == "event":
        if not isinstance(fields.get("kind"), str):
            raise BadRecord("event record needs fields.kind")
        _int(fields.get("created_at"), "fields.created_at")
    if journal_kind != "event" and op != "delete":
        if not isinstance(fields.get("status"), str):
            raise BadRecord("task record needs fields.status")
        _int(fields.get("priority"), "fields.priority", optional=True)
        _int(fields.get("ready_since"), "fields.ready_since", optional=True)


def _lifecycle_payload(kind: str, payload: Any) -> dict:
    if not isinstance(payload, Mapping):
        raise BadRecord("payload must be an object")
    if set(payload) != set(LIFECYCLE_KEYS):
        raise BadRecord(f"{kind} payload keys must be exactly {sorted(LIFECYCLE_KEYS)}")
    journal_kind = payload["journal_kind"]
    if journal_kind not in KINDS or (journal_kind == "opening") != (kind == "opening"):
        raise BadRecord(f"journal_kind {journal_kind!r} does not fit a {kind} record")
    op = payload["op"]
    if op is not None and not isinstance(op, str):
        raise BadRecord("op must be a string or null")
    fields = payload["fields"]
    if not isinstance(fields, Mapping):
        raise BadRecord("fields must be an object")
    allowed = {"kind", "created_at"} if journal_kind == "event" else set(TASK_FIELDS)
    if not set(fields) <= allowed:
        raise BadRecord("fields contains prohibited or unknown content")
    if any(isinstance(value, (Mapping, list, tuple)) for value in fields.values()):
        raise BadRecord("fields values must be JSON scalars")
    _json_safe(fields, "fields")
    _check_fields(journal_kind, op, fields)
    return {"task_id": _str(payload["task_id"], "task_id"), "op": op, "fields": dict(fields),
            "journal_seq": _uint(payload["journal_seq"], "journal_seq", positive=True),
            "journal_kind": journal_kind, "database_uid": _str(payload["database_uid"], "database_uid"),
            "source_uid": _str(payload["source_uid"], "source_uid", optional=True)}


def decode_record(raw: Any) -> dict:
    """A capture line as a checked envelope dict with exactly :data:`ENVELOPE_KEYS`."""
    if not isinstance(raw, Mapping):
        raise BadRecord("record must be an object")
    if set(raw) != set(ENVELOPE_KEYS):
        missing, extra = set(ENVELOPE_KEYS) - set(raw), set(raw) - set(ENVELOPE_KEYS)
        raise BadRecord(f"envelope keys differ: missing {sorted(missing)}, unexpected {sorted(map(str, extra))}")
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != VERSION:
        raise BadRecord(f"unsupported record version {version!r}")
    kind = raw["kind"]
    if not isinstance(kind, str) or kind not in RECORD_KINDS:
        raise BadRecord(f"unknown record kind {kind!r}")
    if raw["stage_index"] is not None:
        raise BadRecord("stage_index must be null in Phase A")
    out: dict[str, Any] = {"version": VERSION}
    for name in ("capture_id", "source_gateway", "board_uid", "source_epoch"):
        out[name] = _str(raw[name], name)
    out["seq"] = _uint(raw["seq"], "seq", positive=True)
    out["kind"] = kind
    out["wall_ns"] = _uint(raw["wall_ns"], "wall_ns")
    out["mono_ns"] = _uint(raw["mono_ns"], "mono_ns", optional=True)
    out["clock_error_ns"] = _uint(raw["clock_error_ns"], "clock_error_ns", optional=True)
    out["tx_uid"] = _str(raw["tx_uid"], "tx_uid", optional=True)
    out["tick_uid"] = _str(raw["tick_uid"], "tick_uid", optional=True)
    out["stage_index"] = None
    payload = _bounded_payload(raw["payload"])
    if kind in LIFECYCLE_KINDS:
        payload = _lifecycle_payload(kind, payload)
    elif kind == "creation":
        from hermes_cli.kanban_fairness_payloads import creation_payload

        payload = creation_payload(payload)
    elif kind == "owner_generation":
        from hermes_cli.kanban_fairness_payloads import urgency_payload

        payload = urgency_payload(payload)
    elif kind == "membership":
        raise BadRecord("membership records are unsupported")
    elif kind == "loss":
        from hermes_cli.kanban_fairness_payloads import loss_payload

        payload = loss_payload(payload)
    out["payload"] = payload
    return out


def make_record(*, capture_id, source_gateway, board_uid, source_epoch, seq, kind, wall_ns, payload,
                mono_ns=None, clock_error_ns=None, tx_uid=None, tick_uid=None, stage_index=None) -> dict:
    """A validated Phase A envelope. Unknown clock readings stay ``None``; nothing is defaulted or estimated."""
    return decode_record({"version": VERSION, "capture_id": capture_id, "source_gateway": source_gateway,
                          "board_uid": board_uid, "source_epoch": source_epoch, "seq": seq, "kind": kind,
                          "wall_ns": wall_ns, "mono_ns": mono_ns, "clock_error_ns": clock_error_ns,
                          "tx_uid": tx_uid, "tick_uid": tick_uid, "stage_index": stage_index, "payload": payload})


def decode_owner(raw: Any) -> dict:
    """One ``owner_authority.jsonl`` line (written by the turn-urgency observer's owner journal).

    ``interval``: a card held (``role`` ``holder``) or was next for (``next``) a land turn from ``start_s`` to
    ``end_s`` (None while still held). ``coverage``: the journal vouches for ``source`` over ``from_s..to_s``;
    ``complete`` false names a chain break, an overflow or an unreadable release."""
    if not isinstance(raw, Mapping) or raw.get("version") != VERSION:
        raise BadRecord("owner record must be a version-1 object")
    kind = raw.get("kind")
    source = raw.get("source")
    if source not in OWNER_SOURCES:
        raise BadRecord(f"unknown owner source {source!r}")
    if kind == "coverage":
        if not isinstance(raw.get("complete"), bool):
            raise BadRecord("coverage.complete must be a boolean")
        return {"kind": kind, "source": source, "from_s": _number(raw.get("from_s"), "from_s"),
                "to_s": _number(raw.get("to_s"), "to_s"), "complete": raw["complete"],
                "reason": raw.get("reason")}
    if kind == "interval":
        task_id = raw.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise BadRecord("interval.task_id must be a nonempty string")
        if raw.get("role") not in ("holder", "next"):
            raise BadRecord(f"unknown owner role {raw.get('role')!r}")
        end = raw.get("end_s")
        return {"kind": kind, "source": source, "task_id": task_id, "role": raw["role"],
                "start_s": _number(raw.get("start_s"), "start_s"),
                "end_s": None if end is None else _number(end, "end_s")}
    raise BadRecord(f"unknown owner record kind {kind!r}")


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadRecord(f"{name} must be a number")
    return float(value)
