"""Execution evidence for a running card on ANOTHER gateway, read from its synced Board replica.

A replica's own clocks (``pulled_at``, fetch time, file mtime) never attest the owner. The executor does: every
running attempt writes ``heartbeat`` (and ``claimed``/``spawned``) task events stamped with its own ``created_at``
and a portable ``uid``, and those events sync to every replica. The newest such event of the card's CURRENT run
dates the run exactly like the owner's ``task_runs.last_heartbeat_at`` dates it locally (180 s validity), so a
dead or partitioned executor expires to unknown on time alone. A replica row with no portable run identity, no
such event, a run that is not current/running, or a guessed owner profile yields no observation (the caller keeps
its explicit unknown). Only ``running`` is projected: a parked replica card has no executor clock.
"""
from __future__ import annotations

import json
import sqlite3

from hermes_cli.execution_observations import epoch, ident

# Executor-written lifecycle events of an attempt; liveness only, never progress.
LIVENESS_KINDS = ("heartbeat", "claimed", "spawned")
_TERMS = ("run_uid", "run_beat")


def select_terms(conn: sqlite3.Connection, alias: str = "t") -> str:
    """SELECT terms for the current run's identity and newest liveness event; NULL on boards lacking the columns.

    Correlated, evaluated only for ``running`` rows, through the ``task_runs`` primary key and the
    ``task_events(run_id, id)`` index: bounded by the open running cards, not the link count."""
    tasks = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    runs = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
    events = {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
    if not ({"current_run_id", "status"} <= tasks and {"id", "task_id", "uid", "status", "ended_at"} <= runs
            and {"task_id", "run_id", "kind", "created_at", "uid"} <= events):
        return ", ".join(f"NULL AS {name}" for name in _TERMS)
    kinds = ",".join(f"'{k}'" for k in LIVENESS_KINDS)
    return (f"CASE WHEN {alias}.status = 'running' THEN (SELECT r.uid FROM task_runs r WHERE r.id = {alias}.current_run_id "
            f"AND r.task_id = {alias}.id AND r.status = 'running' AND r.ended_at IS NULL) END AS run_uid, "
            f"CASE WHEN {alias}.status = 'running' THEN (SELECT json_array(e.created_at, e.uid) FROM task_events e "
            f"WHERE e.run_id = {alias}.current_run_id AND e.task_id = {alias}.id AND e.kind IN ({kinds}) "
            f"ORDER BY e.created_at DESC, e.uid DESC LIMIT 1) END AS run_beat")


def run_evidence(row: dict) -> dict | None:
    """``{'run_id', 'at', 'ref'}`` from a replica link row, or None when anything is missing or malformed."""
    uid, beat = row.get("run_uid"), row.get("run_beat")
    if row.get("status") != "running" or not ident(uid) or not isinstance(beat, str):
        return None
    try:
        at, event_uid = json.loads(beat)
    except (ValueError, TypeError):
        return None
    if not epoch(at) or not ident(event_uid):
        return None
    return {"run_id": uid, "at": at, "ref": "event:" + event_uid}


def raw(row: dict) -> dict | None:
    """v1 running observation for a qualified replica card row (``_replica_run`` set by ``replica_cards``)."""
    run = row.get("_replica_run")
    if not isinstance(run, dict) or row.get("status") != "running":
        return None
    gateway, profile = row.get("gateway"), row.get("owner_profile")
    identity = {"gateway": gateway, "profile": profile, "board": row.get("board"), "kind": "card",
                "work_id": row.get("id"), "run_id": run["run_id"]}
    owner = {"gateway": gateway, "profile": profile}
    if row.get("assignee"):
        owner["assignee"] = row["assignee"]
    at = run["at"]
    value = {"version": 1, "identity": identity, "owner": owner, "state": "running",
             "source": "kanban.replica_run", "source_revision": run["ref"], "evidence_refs": [run["ref"]],
             "observed_at": at, "valid_until": at + 180, "generation_portable": True,
             "last_activity": row.get("activity_at"), "last_activity_kind": row.get("activity_kind"),
             "last_progress": None, "last_progress_kind": None, "last_progress_ref": None, "wait": None}
    request = row.get("human_request")
    if isinstance(request, dict):
        value["human_request"] = {k: v for k, v in request.items() if k not in ("resolved_check", "resolved_check_every")}
    if row.get("stale"):  # an unreadable or long-unpulled replica: its last rows are not a current read
        value.update(state="unknown", last_known_state="running", unknown_reason="source_unavailable")
    return value


def collect(card: dict, evidence: dict) -> None:
    """Add a replica card's running evidence to the counts' per-session evidence sink, keyed like its count key, so
    the sidebar counts and ``session_tasks`` project the same record. Never mutates ``card`` (shared, read-only)."""
    value = raw(card)
    if value is not None:
        evidence.setdefault(card["session"], {})[str(card.get("key"))] = value
