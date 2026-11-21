"""Display-only continuous CI/deploy wait episodes from durable lifecycle history. [daveey]

Wake/claim/checkpoint cycles do not end an unchanged producer wait. A different
producer or explicit lifecycle exit does. Never use card age or volatile render state.
A bounded history with an unobserved beginning must report an unknown start.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3

from hermes_cli.kanban_awaiting import run_heads

HISTORY_LIMIT = 512
PARKS = ("scheduled", "continued", "blocked")
EXITS = ("completed", "archived", "reopened", "gave_up", "triage_swept",
         "block_loop_detected", "contained", "review_requested", "promoted", "status")


def _identity(payload):
    awaiting = payload.get("awaiting")
    if isinstance(awaiting, dict) and not awaiting.get("inferred"):
        kind = awaiting.get("kind")
        kind = "ci" if kind == "git-ci" else kind
        ref = awaiting.get("ref")
        if kind == "external" and isinstance(ref, str):
            match = re.match(r"(ci|git-ci|deploy)\b", ref, re.IGNORECASE)
            kind = ("deploy" if match[1].lower() == "deploy" else "ci") if match else kind
        if kind in ("ci", "deploy") and isinstance(ref, str) and ref.strip():
            # Commit heads are part of identity: a new head is a new CI/deploy run.
            heads = tuple(run_heads({"ref": ref}, payload.get("wake_check")))
            return kind, ref.strip(), str(awaiting.get("gateway") or ""), heads
        return None
    check = payload.get("wake_check")
    if isinstance(check, str) and re.search(r"\bgit-ci\b", check):
        return "ci", check.strip(), "", tuple(run_heads({}, check))
    return None


def _payload(value):
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _stamp(value):
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return None
    return (stamp / 1000 if stamp > 1e11 else stamp) if math.isfinite(stamp) and stamp > 0 else None


def read(conn: sqlite3.Connection, task_id: str):
    """Current ``{kind, since}``, or None for a different wait; no schema writes.

    Read only semantic transitions, newest first, using the indexed task/id history.
    If the cap hides the beginning of the matching episode, retain its kind but
    return since=None. Missing history/table is likewise unknown, never zero.
    """
    kinds = (*PARKS, *EXITS)
    placeholders = ",".join("?" for _ in kinds)
    try:
        rows = conn.execute(
            f"SELECT kind, payload, created_at FROM task_events WHERE task_id=? "
            f"AND kind IN ({placeholders}) ORDER BY id DESC LIMIT ?",
            (task_id, *kinds, HISTORY_LIMIT + 1)).fetchall()
    except sqlite3.Error:
        return None
    if not rows or rows[0][0] not in PARKS:
        return None
    identity = _identity(_payload(rows[0][1]))
    if identity is None:
        return None
    since = None
    for kind, payload, at in rows[:HISTORY_LIMIT]:
        if kind not in PARKS or _identity(_payload(payload)) != identity:
            return {"kind": identity[0], "since": since}
        since = _stamp(at)
    return {"kind": identity[0], "since": None if len(rows) > HISTORY_LIMIT else since}
