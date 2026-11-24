"""Dormant full destination materialization; no RPC/caller activates this yet.

Only an authenticated, gateway-pinned Board handoff response may reach this API.
A supplied dictionary is NOT authentication. Return the rows to the ACK sender
only AFTER its enclosing write transaction commits. No lifecycle hooks, cursor
advancement, budget renewal or release authority is conferred by this import.
"""
from __future__ import annotations

import json
import re
import sqlite3

from hermes_cli import kanban_db_sync as sync


def _validate(conn: sqlite3.Connection, record: dict) -> str:
    if not isinstance(record, dict) or record.get("phase") not in ("committed", "materialized", "complete"):
        raise ValueError("destination needs a committed manifest")
    task, source, destination, nonce, version = (record.get(k) for k in ("task", "source", "destination", "nonce", "expected_version"))
    if (not isinstance(task, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", task)
            or not isinstance(source, str) or not re.fullmatch(r"worker-[a-z0-9-]{1,24}", source)
            or destination != "hub" or not isinstance(nonce, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{16,128}", nonce) or type(version) is not int or version < 1):
        raise ValueError("invalid destination identity")
    state = conn.execute("SELECT gateway_id FROM kanban_sync_state").fetchall()
    if len(state) != 1 or state[0][0] != destination or sync.board_mode(conn) == "off":
        raise ValueError("materialization is not on the destination gateway")
    rows, seen = record.get("rows"), set()
    if not isinstance(rows, list) or not rows:
        raise ValueError("destination manifest is empty")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"table", "uid", "fields"}:
            raise ValueError("invalid destination manifest row")
        table, uid, fields = row["table"], row["uid"], row["fields"]
        if (table not in sync.SYNCED_TABLES or not isinstance(uid, str) or not isinstance(fields, dict)
                or (table, uid) in seen or fields.get("uid") != uid
                or type(fields.get("version")) is not int or fields["version"] < 1
                or not isinstance(fields.get("origin_gateway"), str) or not isinstance(fields.get("hlc"), str)):
            raise ValueError("invalid or duplicate destination row")
        seen.add((table, uid))
        related = (uid == task and fields.get("id") == task if table == "tasks" else
                   task in (fields.get("parent_id"), fields.get("child_id")) if table == "task_links" else
                   fields.get("task_id") == task)
        if not related:
            raise ValueError("unrelated destination manifest row")
        if table == "task_links" and uid != f"{fields.get('parent_id')}>{fields.get('child_id')}":
            raise ValueError("destination link identity changed")
    first = rows[0]
    fields = first["fields"]
    if (first["table"] != "tasks" or sum(r["table"] == "tasks" for r in rows) != 1
            or fields.get("created_on") != destination or fields.get("version") != version + 1
            or fields.get("home_generation") != nonce or fields.get("deleted_at") is not None
            or fields.get("status") in ("running", "done", "archived") or not fields.get("status")):
        raise ValueError("destination canonical task identity changed")
    encoded = json.dumps({**record, "phase": "committed"}, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > 1024 * 1024:
        raise ValueError("destination manifest too large")
    return encoded


def _readback(conn: sqlite3.Connection, record: dict) -> None:
    task = record["task"]
    expected = {table: set() for table in sync.SYNCED_TABLES}
    for entry in record["rows"]:
        table, uid, fields = entry["table"], entry["uid"], entry["fields"]
        if table == "task_links":
            local = conn.execute("SELECT * FROM task_links WHERE parent_id=? AND child_id=?",
                                 (fields["parent_id"], fields["child_id"])).fetchone()
        else:
            local = conn.execute(f"SELECT * FROM {table} WHERE {'id' if table == 'tasks' else 'uid'}=?", (uid,)).fetchone()
        if fields.get("deleted_at") is not None:
            tomb = conn.execute("SELECT 1 FROM kanban_sync_tombstones WHERE table_name=? AND row_uid=?", (table, uid)).fetchone()
            if local is not None or not tomb:
                raise ValueError("destination deleted materialization missing")
            continue
        expected[table].add(uid)
        if local is None:
            raise ValueError("destination materialization row missing")
        for column in set(sync.captured_columns(conn, table)) | (sync.SERVER_COLUMNS.get(table, frozenset()) & set(local.keys())):
            wire = sync._wire_name(table, column)
            if wire not in fields:
                continue
            value = sync._from_wire(fields[wire])
            if column in sync.SECONDS_COLUMNS.get(table, ()):
                value = sync.to_seconds(value)
            if column in sync.JSON_SERVER_COLUMNS.get(table, ()):
                from hermes_cli.kanban_workspace_source import json_column
                value = json_column(value)
            if (table, column) in sync.REF_COLUMNS:
                ref = conn.execute("SELECT id FROM task_runs WHERE uid=?", (value,)).fetchone() if value is not None else None
                if value is not None and ref is None:
                    raise ValueError("destination materialization run missing")
                value = ref[0] if ref else None
            if local[column] != value:
                raise ValueError(f"destination materialization differs: {table}.{column}")
        if table == "tasks" and local["sync_version"] != fields["version"]:
            raise ValueError("destination materialization version differs")
    for table in sync.SYNCED_TABLES:
        if table == "tasks":
            continue
        if table == "task_links":
            found = {f"{r[0]}>{r[1]}" for r in conn.execute("SELECT parent_id,child_id FROM task_links WHERE parent_id=? OR child_id=?", (task, task))}
        else:
            found = {r[0] for r in conn.execute(f"SELECT uid FROM {table} WHERE task_id=?", (task,))}
        if found != expected[table]:
            raise ValueError(f"destination materialization extra or missing rows: {table}")
    generation = conn.execute("SELECT nonce FROM kanban_home_generations1 WHERE task_id=?", (task,)).fetchone()
    if not generation or generation[0] != record["nonce"]:
        raise ValueError("destination materialization epoch missing")


def materialize(conn: sqlite3.Connection, record: dict) -> list[dict]:
    """Import+verify within caller's writer, with rollback even if refusal is caught.

    Existing certified restriction fences stay active. Protected evidence must
    already have its ordinary authenticated certificate; this is not a bypass.
    Saved ACKs are rechecked against actual rows, never used to rewind later work.
    """
    from hermes_cli import kanban_containment_replica as containment

    if not conn.in_transaction:
        raise ValueError("destination materialization requires a write transaction")
    conn.execute("SAVEPOINT home_destination")
    try:
        encoded = _validate(conn, record)
        task = record["task"]
        # Conservative initial implementation: do not overwrite unresolved local
        # intent, including synthetic parents or compatibility-held fields.
        for table in ("kanban_sync_outbox", "kanban_sync_parent_envelopes", "kanban_sync_omissions"):
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise ValueError("destination has pending custody")
        old = conn.execute("SELECT claim_lock,worker_pid,lease_holder,workspace_custody FROM tasks WHERE id=?", (task,)).fetchone()
        if (old and any(v is not None for v in old)) or conn.execute(
                "SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL LIMIT 1", (task,)).fetchone():
            raise ValueError("destination has live custody")
        conn.execute("CREATE TABLE IF NOT EXISTS kanban_home_materialized1(task_id TEXT PRIMARY KEY, nonce TEXT NOT NULL, manifest TEXT NOT NULL)")
        prior = conn.execute("SELECT nonce,manifest FROM kanban_home_materialized1 WHERE task_id=?", (task,)).fetchone()
        if prior:
            if tuple(prior) != (record["nonce"], encoded):
                raise ValueError("destination materialization identity changed")
        else:
            changes = [sync.RemoteChange(seq=r["fields"].get("seq", 0), table=r["table"], uid=r["uid"],
                       op="delete" if r["fields"].get("deleted_at") is not None else "upsert", fields=r["fields"],
                       version=r["fields"]["version"], hlc=r["fields"]["hlc"], origin_gateway=r["fields"]["origin_gateway"])
                       for r in record["rows"]]
            containment.prepare_apply(conn, changes)
            applying = conn.execute("SELECT applying FROM kanban_sync_state").fetchone()[0]
            conn.execute("UPDATE kanban_sync_state SET applying=1")
            result = sync.ApplyResult(cursor=0, historical_import=True)
            for change in changes:
                with containment.projection(conn, change):
                    sync._apply_one(conn, change, record["destination"], result)
            containment.restore_page(conn, changes)
            # No terminal event replay or after-commit lifecycle callbacks: the
            # canonical task snapshot is the status, not historical event intent.
            conn.execute("UPDATE kanban_sync_state SET applying=?", (applying,))
            _readback(conn, record)
            conn.execute("INSERT INTO kanban_home_materialized1(task_id,nonce,manifest) VALUES (?,?,?)", (task, record["nonce"], encoded))
        _readback(conn, record)
    except BaseException:
        conn.execute("ROLLBACK TO home_destination")
        conn.execute("RELEASE home_destination")
        raise
    conn.execute("RELEASE home_destination")
    return record["rows"]
