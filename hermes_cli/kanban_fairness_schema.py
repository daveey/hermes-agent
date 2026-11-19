"""Record shapes, thresholds and exit codes of the queue-fairness evidence capture. [daveey]

One capture is a journal of ``tasks`` row versions and lifecycle events taken from one Kanban database
(normally a board's full replica, so one host sees every gateway's cards), drained to hashed JSONL segments
and closed by a sealed ``manifest.json``. The reader (:mod:`kanban_fairness_metrics`) accepts only records
that decode here; anything else is a coverage gap, never a silently skipped line.
"""
from __future__ import annotations

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


def decode_record(raw: Any) -> dict:
    """A journal line as a checked dict: ``seq, kind, wall_ms, task_id, op, fields``."""
    if not isinstance(raw, Mapping):
        raise BadRecord("record must be an object")
    if raw.get("version") != VERSION:
        raise BadRecord(f"unsupported record version {raw.get('version')!r}")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise BadRecord(f"unknown record kind {kind!r}")
    task_id = raw.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise BadRecord("task_id must be a nonempty string")
    fields = raw.get("fields")
    if not isinstance(fields, Mapping):
        raise BadRecord("fields must be an object")
    op = raw.get("op")
    if kind == "task" and op not in TASK_OPS:
        raise BadRecord(f"unknown task op {op!r}")
    if kind == "event":
        if not isinstance(fields.get("kind"), str):
            raise BadRecord("event record needs fields.kind")
        _int(fields.get("created_at"), "fields.created_at")
    if kind != "event" and op != "delete":
        if not isinstance(fields.get("status"), str):
            raise BadRecord("task record needs fields.status")
        _int(fields.get("priority"), "fields.priority", optional=True)
        _int(fields.get("ready_since"), "fields.ready_since", optional=True)
    return {"seq": _int(raw.get("seq"), "seq"), "kind": kind, "wall_ms": _int(raw.get("wall_ms"), "wall_ms"),
            "task_id": task_id, "op": op, "fields": dict(fields)}


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
