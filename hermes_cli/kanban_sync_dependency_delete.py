"""Audited abandonment of refused dependency DELETEs, never replay or lifecycle recovery.

Preview/apply touch only an immutable host-local receipt and the exact stale link
 tombstone. Tasks, leases, links and history are never written. Board reads are
bounded evidence, not a distributed lock: a changed feed during proof refuses.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_reserved_key as keys
from hermes_cli.kanban_db_connect import write_txn

ERROR = "containment_hold: use scoped resume-contained; generic writes cannot satisfy containment"
TABLE = "kanban_sync_dependency_recoveries"
MAX_ROWS = 10000


class Refused(ValueError):
    """No mutation is authorized by this proof."""


def _require(ok, reason):
    if not ok:
        raise Refused(reason)


def _schema(conn):
    conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (receipt_id TEXT PRIMARY KEY, board TEXT NOT NULL, "
                 "row_uid TEXT NOT NULL UNIQUE, endpoint TEXT NOT NULL, reason TEXT NOT NULL, snapshot_json TEXT NOT NULL, "
                 "snapshot_sha256 TEXT NOT NULL, evidence_json TEXT NOT NULL)")
    for op in ("UPDATE", "DELETE"):
        conn.execute(f"CREATE TRIGGER IF NOT EXISTS dependency_recovery_{op.lower()} BEFORE {op} ON {TABLE} "
                     "BEGIN SELECT RAISE(ABORT,'immutable dependency recovery receipt'); END")


def _rows(conn, sql, args=()):
    result = [dict(r) for r in conn.execute(sql, args).fetchmany(MAX_ROWS + 1)]
    _require(len(result) <= MAX_ROWS, "local_snapshot_too_large")
    return result


def _ids(uid):
    pieces = uid.split(">") if isinstance(uid, str) else []
    _require(len(pieces) == 2 and all(pieces) and pieces[0] != pieces[1], "bad_link_uid")
    return tuple(pieces)


def _snapshot(conn, uid):
    from hermes_cli.kanban_containment_connections import held
    parent, child = _ids(uid)
    return {
        "link": _rows(conn, "SELECT * FROM task_links WHERE parent_id=? AND child_id=?", (parent, child)),
        "tasks": _rows(conn, "SELECT * FROM tasks WHERE id IN (?,?) ORDER BY id", (parent, child)),
        "ops": _rows(conn, "SELECT * FROM kanban_sync_outbox WHERE table_name='task_links' AND row_uid=? ORDER BY op_id", (uid,)),
        "endpoint_ops": _rows(conn, "SELECT * FROM kanban_sync_outbox WHERE table_name='tasks' AND row_uid IN (?,?) "
                              "ORDER BY op_id", (parent, child)),
        "tombstones": _rows(conn, "SELECT * FROM kanban_sync_tombstones WHERE "
                            "(table_name='task_links' AND row_uid=?) OR (table_name='tasks' AND row_uid IN (?,?)) "
                            "ORDER BY table_name,row_uid", (uid, parent, child)),
        "events": [r for r in _rows(conn, "SELECT * FROM task_events WHERE task_id=? AND kind='unlinked' ORDER BY id", (child,))
                   if json.loads(r["payload"] or "null") == {"parent": parent, "child": child}],
        "held": [held(conn, parent), held(conn, child)],
    }


def _local(snap, uid, *, settlement=False):
    parent, child = _ids(uid)
    _require(snap["link"] == [{"parent_id": parent, "child_id": child}], "local_link_changed")
    _require(len(snap["tasks"]) == 2, "missing_endpoint")
    _require(not any(snap["held"]), "local_containment_hold")
    _require(not snap["endpoint_ops"], "endpoint_local_work")
    _require(bool(snap["ops"]), "no_rejected_deletes")
    for op in snap["ops"]:
        _require(op["state"] == "rejected" and op["op"] == "delete" and op["last_error"] == ERROR
                 and json.loads(op["fields"]) == {}, "unsupported_link_work")
    _require(len(snap["events"]) >= len(snap["ops"])
             and all(e.get("uid") for e in snap["events"])
             # The SQL trigger and Python event writer sample separate clocks;
             # float truncation or crossing a second can place the event next second.
             and all(any(o["created_at"] // 1000 <= e["created_at"] <= o["created_at"] // 1000 + 1
                         for e in snap["events"]) for o in snap["ops"]),
             "missing_unlink_audit")
    if settlement:
        _require(not snap["tombstones"], "new_local_tombstone")
    else:
        _require(snap["tombstones"] == [{"table_name": "task_links", "row_uid": uid,
                                        "deleted_at": max(op["created_at"] for op in snap["ops"])}],
                 "uncovered_tombstone")


def _empty(hub, board, cursor):
    data = hub.changes(board, cursor)
    _require(isinstance(data, dict) and data.get("changes") == [] and data.get("more") is False
             and type(data.get("head")) is int and data["head"] == cursor and data.get("next") == cursor,
             "authoritative_feed_changed")
    return data


def _evidence(hub, board, table, uid, head):
    e = hub.row_evidence(board, table, uid)
    _require(isinstance(e, dict) and (e.get("board"), e.get("table"), e.get("uid")) == (board, table, uid)
             and e.get("head") == head and e.get("state") == "live" and e.get("tombstone") is None
             and isinstance(e.get("row"), dict), "authoritative_identity_changed")
    r = e["row"]
    _require(r.get("uid") == uid and r.get("deleted_at") is None
             and type(r.get("version")) is int and r["version"] > 0, "authoritative_version_changed")
    return e


def _proof(conn, hub, board, uid, snap, *, refresh=True):
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_containment_remote import context
    _require(not conn.in_transaction, "network_inside_transaction")
    _require(keys._identity_refusal(conn, hub, board) is None, "sync_identity_changed")
    if refresh:
        path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        pulled = client._pull_replica(board, hub, hub.ident.gateway, path, diagnostic=True)
        _require(not pulled.busy and not pulled.more, "diagnostic_refresh_incomplete")
    shadow = client.open_shadow(board, hub.ident.gateway)
    try:
        state = sync.sync_state(shadow)
        head = state["cursor"]
        _require(_rows(shadow, "SELECT * FROM task_links WHERE parent_id=? AND child_id=?", _ids(uid)) == snap["link"],
                 "shadow_link_changed")
    finally:
        shadow.close()
    empty = _empty(hub, board, head)
    link = _evidence(hub, board, "task_links", uid, head)
    parent, child = _ids(uid)
    _require((link["row"].get("parent_id"), link["row"].get("child_id")) == (parent, child), "canonical_link_changed")
    endpoints = []
    for task in sorted((parent, child)):
        e = _evidence(hub, board, "tasks", task, head)
        local = next(t for t in snap["tasks"] if t["id"] == task)
        _require(e["row"].get("id") == task and e["row"].get("created_on") == local["created_on"]
                 and local["sync_version"] == e["row"]["version"], "endpoint_identity_changed")
        c = context(hub, board, task)
        _require(c["held"] is False and e["row"].get("containment_episode") is None, "canonical_containment_hold")
        endpoints.append(e["row"])
    _empty(hub, board, head)
    return {"link": link["row"], "endpoints": endpoints, "cut": empty}


def _token(hub, board, uid, reason, snap, proof):
    return keys._sha({"v": 1, "board": board, "uid": uid, "endpoint": keys._endpoint(hub), "reason": reason,
                      "snapshot": snap, "link": proof["link"], "endpoints": proof["endpoints"]})


def _prepare(conn, hub, board, uid, reason):
    _require(isinstance(reason, str) and bool(reason.strip()), "reason_required")
    started = keys._mono()
    snap = _snapshot(conn, uid)
    _local(snap, uid)
    proof = _proof(conn, hub, board, uid, snap)
    _require(keys._fresh(started), "evidence_expired")
    _require(_snapshot(conn, uid) == snap, "local_inputs_changed")
    return snap, proof, started, _token(hub, board, uid, reason, snap, proof)


def plan(conn, hub, board, uid, reason):
    """Read-only default; no receipt/schema or canonical writes."""
    from hermes_cli.kanban_sync_client import HubError, Resync, SyncUnavailable
    from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
    try:
        snap, proof, _, token = _prepare(conn, hub, board, uid, reason)
        return {"action": "preview", "eligible": True, "token": token,
                "op_ids": [o["op_id"] for o in snap["ops"]], "evidence": proof}
    except (Refused, HubError, Resync, SyncUnavailable, ContainmentRefused, OSError, ValueError) as exc:
        return {"action": "refused", "eligible": False, "reason": str(exc)}


def apply(conn, hub, board, uid, expect, reason):
    """Insert immutable custody and clear only the covered stale tombstone, atomically."""
    from hermes_cli.kanban_sync_client import HubError, Resync, SyncUnavailable
    from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
    try:
        snap, proof, started, token = _prepare(conn, hub, board, uid, reason)
        _require(isinstance(expect, str) and expect == token, "preview_token_changed")
        with write_txn(conn):
            _require(keys._fresh(started), "evidence_expired")
            _require(keys._identity_refusal(conn, hub, board) is None, "sync_identity_changed")
            _require(_snapshot(conn, uid) == snap, "local_inputs_changed")
            _schema(conn)
            raw = keys._canon(snap)
            conn.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?)",
                         (token, board, uid, keys._endpoint(hub), reason, raw, keys._sha(snap), keys._canon(proof)))
            conn.execute("DELETE FROM kanban_sync_tombstones WHERE table_name='task_links' AND row_uid=? AND deleted_at=?",
                         (uid, snap["tombstones"][0]["deleted_at"]))
        return {"action": "applied", "receipt_id": token, "op_ids": [o["op_id"] for o in snap["ops"]]}
    except (Refused, HubError, Resync, SyncUnavailable, ContainmentRefused, OSError, ValueError) as exc:
        return {"action": "refused", "reason": str(exc)}


def receipt_for_op(conn, op):
    if op["table_name"] != "task_links" or op["op"] != "delete":
        return None
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (TABLE,)).fetchone():
        return None
    matches = _rows(conn, f"SELECT * FROM {TABLE} WHERE row_uid=? ORDER BY receipt_id", (op["row_uid"],))
    return (matches[0] if len(matches) == 1 else {"invalid": "ambiguous dependency receipts"}) if matches else None


def settle(conn, hub, board, op, receipt):
    """Normal sync retires just this byte-identical op; immutable receipt/history stay."""
    from hermes_cli.kanban_sync_client import HubError, Resync, SyncUnavailable
    from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
    report = {"op_id": op["op_id"], "table": op["table_name"], "row_uid": op["row_uid"],
              "classification": "unresolved", "action": "retained"}
    try:
        saved = json.loads(receipt["snapshot_json"])
        _require(keys._sha(saved) == receipt["snapshot_sha256"] and op in saved["ops"], "receipt_bytes_changed")
        _require(receipt["board"] == board and receipt["endpoint"] == keys._endpoint(hub), "receipt_identity_changed")
        started = keys._mono()
        snap = _snapshot(conn, op["row_uid"])
        _local(snap, op["row_uid"], settlement=True)
        _require(all(o in saved["ops"] for o in snap["ops"]), "new_link_work")
        _require(snap["events"] == saved["events"], "history_changed")
        proof = _proof(conn, hub, board, op["row_uid"], snap, refresh=False)
        _require(proof["link"] == json.loads(receipt["evidence_json"])["link"], "receipt_link_changed")
        with write_txn(conn):
            _require(keys._fresh(started), "evidence_expired")
            _require(keys._identity_refusal(conn, hub, board) is None, "sync_identity_changed")
            _require(_snapshot(conn, op["row_uid"]) == snap, "local_inputs_changed")
            _require(receipt_for_op(conn, op) == receipt, "receipt_changed")
            conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id=? AND state='rejected'", (op["op_id"],))
        report.update(classification="superseded", action="retired", proof="immutable dependency recovery receipt")
    except (Refused, HubError, Resync, SyncUnavailable, ContainmentRefused, OSError, ValueError, KeyError, TypeError) as exc:
        report["proof"] = str(exc)
    return report
