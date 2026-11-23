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
from hermes_cli.kanban_table_columns import tables_columns

# Executor-written lifecycle events of an attempt; liveness only, never progress.
LIVENESS_KINDS = ("heartbeat", "claimed", "spawned")

# Bound parameters, never interpolated: the statement text is a constant. Driven from this gateway's links
# (``idx_task_chat_links_chat``) into ``tasks`` by primary key: a running-task-driven EXISTS re-scanned every link of
# the gateway once per running card (77 ms on a real default-board replica vs 2.6 ms).
_RUNS_SQL = (
    "SELECT DISTINCT t.id AS id, "
    "(SELECT r.uid FROM task_runs r WHERE r.id = t.current_run_id AND r.task_id = t.id "
    " AND r.status = 'running' AND r.ended_at IS NULL) AS run_uid, "
    "(SELECT json_array(e.created_at, e.uid) FROM task_events e WHERE e.run_id = t.current_run_id "
    " AND e.task_id = t.id AND e.kind IN (?, ?, ?) ORDER BY e.created_at DESC, e.uid DESC LIMIT 1) AS run_beat "
    "FROM task_chat_links l JOIN tasks t ON t.id = l.task_id "
    "WHERE l.chat_gateway = ? AND l.chat_profile = ? AND l.released = 0 "
    "AND t.status = 'running' AND t.created_on IS NOT NULL AND t.created_on != ?")
assert _RUNS_SQL.count("?") == len(LIVENESS_KINDS) + 3


def read_runs(conn: sqlite3.Connection, gateway: str, profile: str) -> dict[str, dict]:
    """``{task id: {'run_uid', 'run_beat'}}`` for the running replica cards linked to this gateway+profile's chats.

    Read on EVERY replica read, outside the link-row memo: a pulled heartbeat event (and the heartbeat columns it
    bumps) is bookkeeping for ``kanban_activity_revision``, so the replica's content key, and with it the memo and
    the counts fingerprint, never move for it (review t_01a1d0f7c4cc4688). Bounded by this gateway's unreleased links
    (``idx_task_chat_links_chat``, the ``tasks`` and ``task_runs`` primary keys, the ``task_events(run_id, id)``
    index; ~4 ms including the schema probe for 154 links / 3 running cards on a real default-board replica).
    Empty on a replica whose schema lacks the run columns."""
    tasks, runs, events = tables_columns(conn, "tasks", "task_runs", "task_events")  # one row, not one per column
    if not ({"current_run_id", "status", "created_on"} <= tasks
            and {"id", "task_id", "uid", "status", "ended_at"} <= runs
            and {"task_id", "run_id", "kind", "created_at", "uid"} <= events):
        return {}
    return {str(r[0]): {"run_uid": r[1], "run_beat": r[2]}
            for r in conn.execute(_RUNS_SQL, (*LIVENESS_KINDS, gateway, profile, gateway))}


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
    scope = run.get("scope", row)
    gateway, profile = scope.get("gateway"), scope.get("owner_profile")
    identity = {"gateway": gateway, "profile": profile, "board": row.get("board"), "kind": "card",
                "work_id": row.get("id"), "run_id": run["run_id"]}
    owner = {"gateway": gateway, "profile": profile}
    if row.get("assignee"):
        owner["assignee"] = row["assignee"]
    at = run["at"]
    value = {"version": 1, "identity": identity, "owner": owner, "state": "running",
             "source": "kanban.replica_run", "source_revision": run["ref"], "evidence_refs": [run["ref"]],
             "observed_at": at, "valid_until": at + 180, "generation_portable": True,
             "last_activity": run.get("activity_at", row.get("activity_at")),
             "last_activity_kind": run.get("activity_kind", row.get("activity_kind")),
             "last_progress": None, "last_progress_kind": None, "last_progress_ref": None, "wait": None}
    request = row.get("human_request")
    if isinstance(request, dict):
        value["human_request"] = {k: v for k, v in request.items() if k not in ("resolved_check", "resolved_check_every")}
    if row.get("stale"):  # an unreadable or long-unpulled replica: its last rows are not a current read
        value.update(state="unknown", last_known_state="running", unknown_reason="source_unavailable")
    return value


def collect(card: dict, evidence: dict) -> None:
    """Add a remote card's record to the counts' per-session evidence sink, keyed like its count key, so the
    sidebar counts and ``session_tasks`` project the same record. Never mutates ``card`` (shared, read-only).

    Running evidence is ``raw``. Any other remote row (parked, todo, unknown, or running with no current-run beat)
    gets the ``kanban.card`` record ``session_tasks`` builds from the same row (``card_raw``): still unknown with no
    clock, but with the row's last-known state and ask, which the count key alone cannot carry. When either adapter
    fails, the remote row gets the ``failed_card`` record ``add_cards`` projects for it (``adapter_error``, keeping
    its ask). No query, no clock."""
    from hermes_cli.kanban_execution_observations import ADAPTER_ERRORS, card_raw, failed_card_raw
    try:
        value = raw(card)
        if value is None and card.get("remote"):
            value = card_raw(card)
    except ADAPTER_ERRORS:  # only a remote row reaches either adapter
        value = failed_card_raw(card)
    if value is not None:
        evidence.setdefault(card["session"], {})[str(card.get("key"))] = value
