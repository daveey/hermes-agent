"""Conservative selective outbox retirement; rejection audit events are immutable here."""
from __future__ import annotations

import json
import sqlite3

from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_db_connect import write_txn


def _local(conn, table, uid):
    if table == "task_links":
        parent, sep, child = uid.partition(">")
        if not sep:
            return None
        row = conn.execute("SELECT * FROM task_links WHERE parent_id=? AND child_id=?", (parent, child)).fetchone()
    else:
        key = "id" if table == "tasks" else "uid"
        row = conn.execute(f"SELECT * FROM {table} WHERE {key}=?", (uid,)).fetchone()
    return row


def _inputs(conn, rejected):
    """Include translated references and pending/tombstone guards in the compare-and-delete snapshot."""
    from hermes_cli import kanban_sync_client as client
    table, uid = rejected["table_name"], rejected["row_uid"]
    row = _local(conn, table, uid)
    fields = client._row_fields(conn, table, row) if row else None
    original = json.loads(rejected["fields"])
    task = uid if table == "tasks" else (fields or original).get("task_id")
    identities = {(table, uid)}
    if task:
        identities.add(("tasks", task))
    pending, tombs = [], []
    for tab, key in sorted(identities):
        pending.extend(tuple(r) for r in conn.execute(
            "SELECT * FROM kanban_sync_outbox WHERE table_name=? AND row_uid=? AND state='pending'", (tab, key)))
        tombs.extend(tuple(r) for r in conn.execute(
            "SELECT * FROM kanban_sync_tombstones WHERE table_name=? AND row_uid=?", (tab, key)))
    parent = _local(conn, "tasks", task) if task else None
    parent_ops = [tuple(r) for r in conn.execute(
        "SELECT * FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid=?", (task,))] if task else []
    return {"row": dict(row) if row else None, "fields": fields, "task": task,
            "parent": dict(parent) if parent else None, "parent_ops": parent_ops,
            "pending": pending, "tombstones": tombs}


def _evidence(hub, board, table, uid):
    from hermes_cli.kanban_sync_client import SyncUnavailable
    e = hub.row_evidence(board, table, uid)
    if (e.get("table"), e.get("uid")) != (table, uid) or not isinstance(e.get("head"), int):
        raise SyncUnavailable("authoritative evidence identity/head mismatch")
    return e


def _classify(op, before, evidence, parent_evidence):
    from hermes_cli import kanban_sync_client as client
    if before["pending"] or before["tombstones"]:
        return "unresolved", "pending local work or local tombstone"
    fields = client._wire_fields(op["table_name"], json.loads(op["fields"]))
    remote, local = evidence.get("row"), before["row"]
    if op["op"] in ("upsert", "import") and evidence.get("state") == "live" and remote and local:
        version = remote.get("version")
        if (remote.get("uid") != op["row_uid"] or not isinstance(version, int)
                or version < (op.get("base_version") or 0)):
            return "unresolved", "authoritative identity/version does not cover rejected write"
        if op["table_name"] in ("tasks", "task_runs") and local.get("sync_version") != version:
            return "unresolved", "local row has not settled at authoritative version"
        if any(table == op["table_name"] and local.get(column) is not None
               and before["fields"].get(wire) is None
               for (table, column), wire in sync.REF_COLUMNS.items()):
            return "unresolved", "local run reference cannot be translated"
        if fields and all(k in before["fields"] and k in remote and before["fields"][k] == remote[k] for k in fields):
            return "superseded", "authoritative row equals current local values for every rejected wire field"
    if (op["table_name"] == "task_chat_links" and op["op"] in ("upsert", "import")
            and parent_evidence and parent_evidence.get("state") == "never_existed"
            and parent_evidence.get("floor") == 0 and parent_evidence.get("row") is None
            and parent_evidence.get("tombstone") is None and before["parent"] is None
            and not before["parent_ops"] and evidence.get("state") == "never_existed"
            and evidence.get("floor") == 0 and evidence.get("row") is None
            and evidence.get("tombstone") is None):
        return "moot", "authoritative unpruned history has no task/link; no local task, create or tombstone"
    return "unresolved", "no proof of supersession or mootness; do not replay historical intent"


def resolve_rejections(conn: sqlite3.Connection, hub, board: str) -> list[dict]:
    """Fresh service reads, followed by transactional revalidation; never trust a replica or replay old writes.

    A legacy service without the evidence route leaves rejections visible. Network calls run outside
    the write transaction. Any local change during the read vetoes retirement until the next round.
    """
    from hermes_cli.kanban_sync_client import HubError, SyncUnavailable
    ops = [dict(r) for r in conn.execute("SELECT * FROM kanban_sync_outbox WHERE state='rejected' ORDER BY op_id")]
    reports = []
    for op in ops:
        report = {"op_id": op["op_id"], "table": op["table_name"], "row_uid": op["row_uid"],
                  "fields": sorted(json.loads(op["fields"])), "classification": "unresolved", "action": "retained"}
        reports.append(report)
        if op["table_name"] not in sync.SYNCED_COLUMNS:
            report["proof"] = "unknown synced table"
            continue
        before = _inputs(conn, op)
        try:
            evidence = _evidence(hub, board, op["table_name"], op["row_uid"])
            parent = _evidence(hub, board, "tasks", before["task"]) if (
                op["table_name"] == "task_chat_links" and before["task"]) else None
        except (HubError, SyncUnavailable, OSError, AttributeError, ValueError) as exc:
            report["proof"] = f"authoritative evidence unavailable: {exc}"
            continue
        report["evidence"] = evidence
        if parent:
            report["parent_evidence"] = parent
        classification, proof = _classify(op, before, evidence, parent)
        report.update(classification=classification, proof=proof)
        if classification == "unresolved":
            continue
        with write_txn(conn):
            current = conn.execute("SELECT * FROM kanban_sync_outbox WHERE op_id=?", (op["op_id"],)).fetchone()
            if current is None or dict(current) != op or _inputs(conn, op) != before:
                report.update(classification="unresolved", proof="local state changed during evidence read")
                continue
            conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id=? AND state='rejected'", (op["op_id"],))
            report["action"] = "retired"
    return reports
