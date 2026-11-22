"""Immutable own-run retain transport in existing first-wins command custody. [daveey]

Enqueue/ACK is not terminal authority. Only independently verified canonical
receipt readback may settle the admitted actor; this module never renews spend.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_failure_release as release


def canonical(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def capability(hub, board: str) -> dict:
    """Probe the serving DO before every send, never cached across rollback."""
    value = hub.decision_retain_capability(board)
    if (not isinstance(value, dict) or type(value.get("decision_retain")) is not int
            or value["decision_retain"] != 1 or type(value.get("frozen_replay")) is not int
            or value["frozen_replay"] != 1 or not isinstance(value.get("hlc"), str)
            or not re.fullmatch(r"[0-9]{13}-[0-9]{4}-server", value["hlc"])):
        raise ValueError("retain unsupported: deployment owner must deploy Board decision-retain v1 before retry")
    return value


def command(conn: sqlite3.Connection, run_uid: str) -> dict | None:
    """Read durable custody after ACK without pretending legacy rows attest bytes."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_failure_commands'").fetchone():
        return None
    row = conn.execute("SELECT * FROM kanban_failure_commands WHERE run_uid=?", (run_uid,)).fetchone()
    return dict(row) if row is not None else None


def frozen_wire(row: dict) -> dict:
    """No HLC enrichment or new identity is allowed on replayed semantic bytes."""
    value = row["fields"] if isinstance(row["fields"], dict) else json.loads(row["fields"])
    if (not isinstance(value, dict) or value.get("op") != "retain"
            or value.get("client_op_id") != row["op_id"] or value.get("task") != row["row_uid"]):
        raise ValueError("retain outbox differs from its immutable request")
    return value


def outbound(conn: sqlite3.Connection, row: dict) -> dict:
    """Validate actual durable custody, not just the mutable transport projection."""
    wire = frozen_wire(row)
    owned = command(conn, wire.get("run_uid"))
    if owned is not None:
        _replayable(owned)
    if (owned is None or owned.get("operation_kind") != "retain" or owned["op_id"] != row["op_id"]
            or owned["task_id"] != row["row_uid"] or owned["request_json"] != canonical(wire)
            or owned["request_sha256"] != hashlib.sha256(owned["request_json"].encode("utf-8")).hexdigest()):
        raise ValueError("retain outbox lost its exact immutable command custody")
    return wire


def _replayable(owned: dict) -> None:
    if owned.get("delivery_state") == "rejected" or owned.get("rejected_release_op_id"):
        raise ValueError("retain rejected: immutable custody retained; ordinary failure recovery owns this run")


def before_send(conn: sqlite3.Connection, rows: list[dict], hub, board: str) -> None:
    retained = [row for row in rows if row["op"] == "retain"]
    if retained:
        capability(hub, board)
        for row in retained:
            outbound(conn, row)


def own_batch(rows: list[dict]) -> list[dict]:
    """Flush preceding captured writes first; retain must have its own response/cache fate."""
    for i, row in enumerate(rows):
        if row["op"] == "retain":
            return rows[:i] if i else rows[:1]
    return rows


def record_result(conn: sqlite3.Connection, row: dict, result: dict) -> str:
    """An ACK may remove transport, never custody or independent-readback requirement."""
    status = result.get("status")
    op_id = row["op_id"]
    prior = conn.execute("SELECT delivery_state FROM kanban_failure_commands WHERE op_id=? AND operation_kind='retain'",
                         (op_id,)).fetchone()
    if prior is not None and prior[0] in ("rejected", "confirmed", "acknowledged"):
        return "deferred"  # A delayed reply cannot revoke evidence of application or rejection.
    from hermes_cli.kanban_sync_compat import transient_rejection
    if status == "rejected" and transient_rejection(result):
        status = "deferred"  # Infrastructure/ordering refusal is not a definitive decision.
    if status == "applied":
        conn.execute("DELETE FROM kanban_sync_outbox WHERE op_id=?", (op_id,))
        state, count = "acknowledged", "applied"
    elif status == "rejected":
        conn.execute("UPDATE kanban_sync_outbox SET state='rejected',attempts=attempts+1,last_error=? WHERE op_id=?",
                     (str(result.get("reason") or "retain rejected")[:500], op_id))
        state, count = "rejected", "rejected"
    else:
        conn.execute("UPDATE kanban_sync_outbox SET attempts=attempts+1,retry_after=? WHERE op_id=?",
                     (int(time.time()) + 30, op_id))
        state, count = "unknown", "deferred"
    conn.execute("UPDATE kanban_failure_commands SET delivery_state=? WHERE op_id=? AND operation_kind='retain'",
                 (state, op_id))
    return count


def _evidence(hub, board: str, table: str, uid: str) -> dict:
    proof = hub.row_evidence(board, table, uid)
    if (proof.get("board") != board or proof.get("table") != table or proof.get("uid") != uid
            or proof.get("state") != "live" or not isinstance(proof.get("row"), dict)):
        raise ValueError("retain canonical evidence unavailable or changed identity")
    return proof["row"]


def confirm(conn: sqlite3.Connection, hub, board: str, run_uid: str) -> dict:
    """Replay frozen intent, then independently verify immutable receipt and ended run."""
    owned = command(conn, run_uid)
    if owned is None or owned.get("operation_kind") != "retain":
        raise ValueError("retain command custody unavailable")
    _replayable(owned)
    wire = json.loads(owned["request_json"])
    if hashlib.sha256(canonical(wire).encode("utf-8")).hexdigest() != owned["request_sha256"]:
        raise ValueError("retain command digest changed")
    capability(hub, board)
    reply = hub.ops(board, [wire])
    results = reply.get("results", [])
    if (len(results) == 1 and results[0].get("client_op_id") == owned["request_uid"]
            and results[0].get("status") == "rejected"):
        from hermes_cli import kanban_db as kb
        with kb.write_txn(conn, allow_nested=True):
            record_result(conn, {"op_id": owned["op_id"]}, results[0])
    if (len(results) != 1 or results[0].get("client_op_id") != owned["request_uid"]
            or results[0].get("status") != "applied"):
        raise ValueError("retain intent not canonically applied; preserve exact command custody")
    event = _evidence(hub, board, "task_events", owned["event_uid"])
    receipt = json.loads(event.get("payload") or "{}")
    expected = {"version": 1, "task_uid": owned["task_id"], "run_uid": run_uid,
                "request_uid": owned["request_uid"], "event_uid": owned["event_uid"],
                "request_sha256": owned["request_sha256"], "operation_kind": "retain",
                "actor_gateway": hub.ident.gateway, "disposition": wire["payload"]}
    if (any(receipt.get(key) != value for key, value in expected.items())
            or event.get("kind") != "decision_retained" or event.get("version") != 1
            or event.get("origin_gateway") != hub.ident.gateway or event.get("task_id") != owned["task_id"]
            or event.get("run_uid") != run_uid or not receipt.get("episode_uid")):
        raise ValueError("retain canonical receipt does not attest the frozen request")
    stop = _evidence(hub, board, "task_events", receipt["episode_uid"])
    run = _evidence(hub, board, "task_runs", run_uid)
    task = _evidence(hub, board, "tasks", owned["task_id"])
    outcome = "safety_refused" if receipt.get("superseded_by_safety_stop") is True else "retained"
    if (stop.get("task_id") != owned["task_id"] or stop.get("kind") != "spend_budget_triaged"
            or run.get("task_id") != owned["task_id"] or run.get("ended_at") is None
            or run.get("outcome") != outcome or task.get("current_run_uid") == run_uid):
        raise ValueError("retain canonical run or stop is not terminal")
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn, allow_nested=True):
        changed = conn.execute("UPDATE kanban_failure_commands SET canonical_receipt_json=?,delivery_state='confirmed' "
                               "WHERE run_uid=? AND request_sha256=? AND delivery_state!='rejected' "
                               "AND rejected_release_op_id IS NULL",
                               (canonical(receipt), run_uid, owned["request_sha256"])).rowcount
        if not changed:
            raise ValueError("retain custody changed during canonical confirmation")
        conn.execute('DELETE FROM kanban_sync_outbox WHERE op_id=?', (owned['op_id'],))
    return {"ok": True, "task_id": owned["task_id"], "run_uid": run_uid,
            "request_uid": owned["request_uid"], "event_uid": owned["event_uid"],
            "episode_uid": receipt["episode_uid"], "status": "triage", "terminal_for_run": True,
            "dependency_satisfied": False, "next_action": receipt.get("next_action"),
            "disposition": "superseded_by_safety_stop" if outcome == "safety_refused" else "retained"}


def _envelope(task_id: str, run_uid: str, payload: dict, expected_episode: str | None,
              request_uid: str, event_uid: str) -> dict:
    return {"client_op_id": request_uid, "op": "retain", "task": task_id, "run_uid": run_uid,
            "event_uid": event_uid, "expected_episode": expected_episode, "payload": payload}


def confirmed_command(conn, task_id: str, run_uid: str) -> bool:
    owned = command(conn, run_uid)
    if (not owned or owned.get('delivery_state') != 'confirmed'
            or owned.get('task_id') != task_id or not owned.get('canonical_receipt_json')):
        return False
    receipt = json.loads(owned['canonical_receipt_json'])
    if owned.get('operation_kind') != 'retain':
        return (receipt.get('verified_safety_terminal') is True and receipt.get('operation_kind') == 'release'
                and receipt.get('task_uid') == task_id and receipt.get('run_uid') == run_uid
                and receipt.get('op_id') == owned['op_id'])
    return (receipt.get('task_uid') == task_id and receipt.get('run_uid') == run_uid
            and receipt.get('request_uid') == owned['request_uid'] and receipt.get('event_uid') == owned['event_uid']
            and receipt.get('request_sha256') == owned['request_sha256']
            and hashlib.sha256(owned['request_json'].encode('utf-8')).hexdigest() == owned['request_sha256'])


def project(conn, hub, board: str, result: dict) -> None:
    """Project exact authenticated evidence without advancing the ordinary feed cursor."""
    from hermes_cli import kanban_sync_client as client
    task_id, run_uid = result['task_id'], result['run_uid']
    changes = [client._row_change(table, _evidence(hub, board, table, uid)) for table, uid in
               [('task_events', result['episode_uid']), ('task_events', result['event_uid']),
                ('task_runs', run_uid), ('tasks', task_id)]]
    sync.apply_remote_changes(conn, changes, new_cursor=sync.sync_state(conn)['cursor'], board=board)
    from hermes_cli.kanban_release_claim import settle_retained
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn, allow_nested=True):
        settle_retained(conn, task_id, run_uid)


def reserve(conn: sqlite3.Connection, task_id: str, run_uid: str, payload: dict,
            expected_episode: str | None) -> dict:
    """Persist immutable wire bytes before any I/O, inside the actor's custody txn.

    First committed release/retain wins. Return a release owner as UNKNOWN to the
    caller, never a substituted retain or a successful terminal disposition.
    """
    if sync.board_mode(conn) != "primary" or not conn.in_transaction:
        raise ValueError("retain reservation requires the primary write transaction")
    row = conn.execute("SELECT task_id FROM task_runs WHERE uid=?", (run_uid,)).fetchone()
    if row is None or row[0] != task_id:
        raise ValueError("retain reservation requires the exact task/run identity")
    release._ensure_commands(conn)
    old = command(conn, run_uid)
    if old is not None:
        if old["operation_kind"] == "retain":
            _replayable(old)
            requested = canonical(_envelope(task_id, run_uid, payload, expected_episode,
                                             old["request_uid"], old["event_uid"]))
            if old["request_json"] != requested:
                raise ValueError("retain request changed after first committed command")
        return old
    request_uid, event_uid = sync.uuid7(), sync.uuid7()
    wire = canonical(_envelope(task_id, run_uid, payload, expected_episode, request_uid, event_uid))
    digest = hashlib.sha256(wire.encode("utf-8")).hexdigest()
    conn.execute("INSERT INTO kanban_failure_commands "
                 "(run_uid,task_id,op_id,operation_kind,request_uid,event_uid,request_json,request_sha256,delivery_state) "
                 "VALUES (?,?,?,'retain',?,?,?,?,'pending')",
                 (run_uid, task_id, request_uid, request_uid, event_uid, wire, digest))
    conn.execute("INSERT INTO kanban_sync_outbox (op_id,table_name,row_uid,op,fields,hlc,created_at) "
                 f"VALUES (?,'tasks',?,'retain',?,'',{sync._NOW_MS_SQL})", (request_uid, task_id, wire))
    return command(conn, run_uid)
