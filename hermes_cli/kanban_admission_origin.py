"""Stable claim-time origin attribution, independent of current shepherds."""
import json


def pickup_links(replica, task_id):
    """Carry original chat custody before claim, using pickup's already-open replica."""
    from hermes_cli import kanban_db_sync as sync, kanban_sync_client as ksc

    return [sync.RemoteChange(0, "task_chat_links", row["uid"], "upsert",
                              ksc._row_fields(replica, "task_chat_links", row), 0, "", "")
            for row in replica.execute("SELECT * FROM task_chat_links WHERE task_id=?", (task_id,))]


def origin_key(conn, task_id):
    """Use the original created chat, including released links, then creating gateway/creator."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        row = conn.execute(
            "SELECT chat_gateway, chat_profile, chat_session FROM task_chat_links "
            "WHERE task_id=? AND kind='created' ORDER BY created_at, uid LIMIT 1", (task_id,),
        ).fetchone()
        if row:
            return json.dumps(["chat", row[0], row[1] or "default", row[2]], separators=(",", ":"))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    gateway = "created_on" if "created_on" in cols else "NULL"
    row = conn.execute(f"SELECT {gateway}, created_by FROM tasks WHERE id=?", (task_id,)).fetchone()
    return json.dumps(["creator", row[0] or "local", row[1] or "unknown"], separators=(",", ":"))
