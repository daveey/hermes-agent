"""Retire a containment-refused metadata classification without replaying it. [daveey]

``classify_parked_request`` (``hermes kanban block --metadata-only``, dashboard) used to append a ``blocked``
human_request event to a contained card. The Board refuses every generic write while a containment episode holds
(``containment_hold``), so that upload can never apply inside the episode, and replaying historical intent after the
episode ends is forbidden. Before this module the rejection stayed in the outbox forever and kept
``hermes kanban sync diff`` dirty.

The rejected upload is moot only when all of this holds on fresh evidence, revalidated in the deleting transaction:

- the op is the exact refused generic ``task_events`` upsert, and the local event still equals its frozen fields;
- the event is the producer's metadata-only, classified-in-place ``blocked`` event: no run, no lifecycle;
- authoritative row evidence says the Board never stored that event uid (``never_existed``, floor 0, no tombstone),
  under any client op id, so an ambiguous frozen replay identity does not matter: nothing is replayed;
- the card is parked (triage/blocked) with no run, claim or human_request locally and canonically, at the same
  settled version and status: the request the event documents was applied nowhere;
- the same containment episode is still the card's marker locally and canonically, began before the event, has no
  release or closure, and the local containment guard still holds the card.

The original outbox row is copied into an immutable local receipt before its exact rejected queue entry is removed.
The local event row, task row, containment history, audits and fences are untouched; nothing is sent to the Board.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from hermes_cli.kanban_release import CONTAINMENT

CLASSIFICATION = "containment_refused_classification"
PROOF = ("the Board never stored this metadata-only classification refused by a containment episode that still "
         "holds; canonical and local cards carry no request; original op archived, never replayed")
_FIELDS = frozenset({"task_id", "run_uid", "kind", "payload", "created_at"})
_EPISODE_KINDS = ("contained", "containment_released", "containment_closed")


def _object(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def snapshot(conn: sqlite3.Connection, op: dict, task) -> dict | None:
    """Containment history and the local hold, part of the resolver's compare-and-delete snapshot."""
    if (op["table_name"] != "task_events" or op["op"] != "upsert" or op.get("last_error") != CONTAINMENT
            or not task):
        return None
    from hermes_cli.kanban_containment_guard import pending
    events = [dict(r) for r in conn.execute(
        "SELECT uid, kind, payload, created_at FROM task_events WHERE task_id=? AND kind IN (?,?,?) ORDER BY id",
        (task, *_EPISODE_KINDS))]
    return {"events": events, "held": bool(pending(conn, task))}


def moot(op: dict, before: dict, evidence: dict, parent_evidence: dict | None) -> bool:
    stamp = before.get("containment_event")
    if (not stamp or op["table_name"] != "task_events" or op["op"] != "upsert" or op.get("state") != "rejected"
            or op.get("last_error") != CONTAINMENT or before["pending"] or before["tombstones"]
            or before["receipt"] or before["parent_ops"] or stamp.get("held") is not True):
        return False
    fields = _object(op["fields"])
    if fields is None or fields.keys() != _FIELDS or before["fields"] != fields:
        return False
    payload = _object(fields["payload"])
    if (fields["kind"] != "blocked" or fields["run_uid"] is not None or fields["task_id"] != before["task"]
            or type(fields["created_at"]) is not int or payload is None
            or payload.get("metadata_only") is not True or payload.get("classified_in_place") is not True
            or not isinstance(payload.get("human_request"), dict)):
        return False
    row, parent = before["row"], before["parent"]
    if not row or row.get("uid") != op["row_uid"] or not parent or parent.get("id") != before["task"]:
        return False
    episode, status, version = parent.get("containment_episode"), parent.get("status"), parent.get("sync_version")
    if (status not in ("triage", "blocked") or not isinstance(episode, str) or not episode
            or type(version) is not int or parent.get("human_request") is not None
            or any(parent.get(k) is not None for k in ("current_run_id", "claim_lock", "worker_pid"))):
        return False
    if not (evidence.get("state") == "never_existed" and evidence.get("floor") == 0
            and evidence.get("row") is None and evidence.get("tombstone") is None):
        return False
    canonical = (parent_evidence or {}).get("row")
    if (not parent_evidence or parent_evidence.get("state") != "live" or parent_evidence.get("tombstone") is not None
            or not isinstance(canonical, dict) or canonical.get("uid") != before["task"]
            or canonical.get("version") != version or canonical.get("status") != status
            or canonical.get("containment_episode") != episode or canonical.get("human_request") is not None
            or canonical.get("current_run_uid") is not None or canonical.get("lease_holder") is not None):
        return False
    starts = [e for e in stamp["events"] if e["kind"] == "contained" and e["uid"] == episode]
    ends = [e for e in stamp["events"] if e["kind"] != "contained"
            and (_object(e["payload"]) or {}).get("episode") in (episode, None)]
    return (len(starts) == 1 and type(starts[0]["created_at"]) is int
            and starts[0]["created_at"] <= fields["created_at"] and not ends)


def archive(conn: sqlite3.Connection, op: dict, before: dict, evidence: dict, parent_evidence: dict) -> bool:
    """Insert the immutable receipt inside the caller's deleting transaction; never replace an existing one."""
    conn.execute("""CREATE TABLE IF NOT EXISTS kanban_rejected_containment_event_receipts (
        op_id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
        evidence_json TEXT NOT NULL)""")
    for verb in ("UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS rejected_containment_event_{verb.lower()} BEFORE {verb} "
                     "ON kanban_rejected_containment_event_receipts BEGIN "
                     "SELECT RAISE(ABORT,'rejected_containment_event_immutable'); END")
    conn.execute("CREATE TRIGGER IF NOT EXISTS rejected_containment_event_insert BEFORE INSERT "
                 "ON kanban_rejected_containment_event_receipts WHEN EXISTS (SELECT 1 FROM "
                 "kanban_rejected_containment_event_receipts WHERE op_id=NEW.op_id) BEGIN "
                 "SELECT RAISE(ABORT,'rejected_containment_event_immutable'); END")
    if conn.execute("SELECT 1 FROM kanban_rejected_containment_event_receipts WHERE op_id=?",
                    (op["op_id"],)).fetchone():
        return False
    text = json.dumps({"operation": op, "event": before["row"], "task": before["parent"],
                       "containment": before["containment_event"], "lifecycle_authority": False},
                      sort_keys=True, separators=(",", ":"), default=str)
    conn.execute("INSERT INTO kanban_rejected_containment_event_receipts VALUES (?,?,?,?)",
                 (op["op_id"], text, hashlib.sha256(text.encode("utf-8")).hexdigest(),
                  json.dumps({"event": evidence, "task": parent_evidence}, sort_keys=True,
                             separators=(",", ":"), default=str)))
    return True
