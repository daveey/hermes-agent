"""Stable claim-time origin attribution, independent of current shepherds."""
import json


def record_pickup_origins(conn, replica, task_ids):
    """Store each picked-up foreign card's origin, read from pickup's already-open replica, in the local-only
    ``tasks.admission_origin``: another gateway's chat links never enter the live copy, and the claim gate must not
    open a replica under ``admission.lock``."""
    from hermes_cli.kanban_db_connect import write_txn

    keys = [(origin_key(replica, tid), tid) for tid in task_ids]
    if keys:
        with write_txn(conn):
            conn.executemany("UPDATE tasks SET admission_origin=? WHERE id=?", keys)


def origin_key(conn, task_id):
    """Use the pickup-time origin, else the original created chat (released links included), else creating
    gateway/creator."""
    from hermes_cli.machine_metrics import table_columns
    cols = table_columns(conn, "tasks")  # runs under admission.lock: one row step, not one per column
    if "admission_origin" in cols:
        row = conn.execute("SELECT admission_origin FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row and row[0]:
            return row[0]
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        row = conn.execute(
            "SELECT chat_gateway, chat_profile, chat_session FROM task_chat_links "
            "WHERE task_id=? AND kind='created' ORDER BY created_at, uid LIMIT 1", (task_id,),
        ).fetchone()
        if row:
            return json.dumps(["chat", row[0], row[1] or "default", row[2]], separators=(",", ":"))
    gateway = "created_on" if "created_on" in cols else "NULL"
    row = conn.execute(f"SELECT {gateway}, created_by FROM tasks WHERE id=?", (task_id,)).fetchone()
    return json.dumps(["creator", row[0] or "local", row[1] or "unknown"], separators=(",", ":"))
