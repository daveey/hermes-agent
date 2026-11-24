"""Durable source STOP, called only with an authenticated Board handoff reply.

No network or activation caller exists yet. A dictionary is not authentication:
production orchestration must obtain it through the gateway-pinned Board RPC.
Only the administrative home and version change; local probe/origin/file custody
stays here. A later worker execution still needs its ordinary real Board lease.
"""
from __future__ import annotations

import json
import sqlite3


def _receipt(conn: sqlite3.Connection, record: dict) -> tuple[dict, dict, str]:
    from hermes_cli import kanban_db_sync as sync

    if not isinstance(record, dict) or record.get("phase") not in ("materialized", "complete"):
        raise ValueError("destination materialization is not acknowledged")
    task = record.get("task")
    prepared = conn.execute("SELECT task_id,source,destination,nonce,expected_version "
                            "FROM kanban_home_prepared1 WHERE task_id=?", (task,)).fetchone()
    keys = ("task", "source", "destination", "nonce", "expected_version")
    if (not prepared or tuple(prepared) != tuple(record.get(k) for k in keys)
            or type(record.get("expected_version")) is not int):
        raise ValueError("source stop identity changed")
    state = conn.execute("SELECT gateway_id FROM kanban_sync_state").fetchall()
    if len(state) != 1 or state[0][0] != record["source"]:
        raise ValueError("source stop is not on the source gateway")
    rows = record.get("rows")
    if (not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows)
            or any(not isinstance(r.get("table"), str) or not isinstance(r.get("uid"), str) for r in rows)
            or len({(r["table"], r["uid"]) for r in rows}) != len(rows)):
        raise ValueError("source stop needs the full unique manifest")
    for row in rows:
        table, uid, fields = row.get("table"), row.get("uid"), row.get("fields")
        if table not in sync.SYNCED_TABLES or not isinstance(uid, str) or not isinstance(fields, dict):
            raise ValueError("source stop manifest invalid")
        related = (uid == task and fields.get("id") == task if table == "tasks" else
                   task in (fields.get("parent_id"), fields.get("child_id")) if table == "task_links" else
                   fields.get("task_id") == task)
        if not related:
            raise ValueError("source stop manifest identity changed")
    first = rows[0]
    if (first["table"] != "tasks" or first["uid"] != task or sum(r["table"] == "tasks" for r in rows) != 1
            or first["fields"].get("created_on") != record["destination"]
            or type(first["fields"].get("version")) is not int
            or first["fields"]["version"] != record["expected_version"] + 1
            or first["fields"].get("deleted_at") is not None):
        raise ValueError("source stop canonical home or version changed")
    payload = {k: record[k] for k in keys}
    payload.update(phase="materialized", rows=rows)
    return payload, first["fields"], json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def finalize_source(conn: sqlite3.Connection, record: dict) -> dict:
    """Atomically install epoch, stop local ownership and retain original custody.

    Savepoint rollback includes the release receipt. A crash cannot commit an
    unfenced old home or a new epoch without the matching immutable receipt.
    An exact retry after an unknown result never rewrites a newer local row.
    """
    from hermes_cli import kanban_db_sync as sync, kanban_home_generation as generation

    if not conn.in_transaction:
        raise ValueError("source stop requires a write transaction")
    conn.execute("SAVEPOINT home_source_stop")
    try:
        payload, fields, encoded = _receipt(conn, record)
        task, nonce = payload["task"], payload["nonce"]
        old = conn.execute("SELECT nonce,receipt FROM kanban_home_finalized1 WHERE task_id=?", (task,)).fetchone()
        if old:
            if tuple(old) != (nonce, encoded):
                raise ValueError("source stop receipt changed")
            current = conn.execute("SELECT created_on,sync_version FROM tasks WHERE id=?", (task,)).fetchone()
            epoch = conn.execute("SELECT nonce FROM kanban_home_generations1 WHERE task_id=?", (task,)).fetchone()
            if (not current or current[0] != payload["destination"] or type(current[1]) is not int
                    or current[1] < fields["version"] or not epoch or epoch[0] != nonce):
                raise ValueError("source stop receipt is not materialized locally")
        else:
            current = conn.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
            if (not current or current["created_on"] != payload["source"]
                    or current["sync_version"] != payload["expected_version"]):
                raise ValueError("source stop local home or version changed")
            # A home move must not incidentally replay events or overwrite any
            # business value. Prove every captured task field against the reply.
            for column in sync.captured_columns(conn, "tasks"):
                wire = sync._wire_name("tasks", column)
                value = current[column]
                if ("tasks", column) in sync.REF_COLUMNS:
                    run = conn.execute("SELECT uid FROM task_runs WHERE id=?", (value,)).fetchone()
                    value = run[0] if run else None
                if wire not in fields or sync._from_wire(fields[wire]) != value:
                    raise ValueError(f"source stop changed task field: {column}")
            for table in ("kanban_sync_outbox", "kanban_sync_parent_envelopes", "kanban_sync_omissions"):
                if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    raise ValueError("source stop requires drained pending custody")
            generation.install(conn)
            epoch = conn.execute("SELECT nonce FROM kanban_home_generations1 WHERE task_id=?", (task,)).fetchone()
            if epoch and epoch[0] != nonce:
                raise ValueError("source stop generation changed")
            # This receipt is the sole local release condition. Receipt, epoch
            # and home update share ONE write transaction: no peer can capture
            # intent between the receipt insertion and epoch installation.
            conn.execute("INSERT INTO kanban_home_finalized1(task_id,nonce,receipt) VALUES (?,?,?)", (task, nonce, encoded))
            generation.mirror_feed(conn, task, {"home_generation": nonce}, fields["version"])
            applying = conn.execute("SELECT applying FROM kanban_sync_state").fetchone()[0]
            conn.execute("UPDATE kanban_sync_state SET applying=1")
            conn.execute("UPDATE tasks SET created_on=?,sync_version=? WHERE id=?",
                         (payload["destination"], fields["version"], task))
            conn.execute("UPDATE kanban_sync_state SET applying=?", (applying,))
    except BaseException:
        conn.execute("ROLLBACK TO home_source_stop")
        conn.execute("RELEASE home_source_stop")
        raise
    conn.execute("RELEASE home_source_stop")
    return {"task": payload["task"], "nonce": payload["nonce"], "phase": "stopped_local"}
