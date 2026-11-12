"""Stable claim-time origin attribution, independent of current shepherds."""
import json


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
