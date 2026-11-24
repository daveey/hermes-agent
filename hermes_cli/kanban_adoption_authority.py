"""Read-only ownership evidence for explicit local adoption. [daveey]

Extracted from the recovery source without its authority-less-create policy.
These helpers neither create cards nor infer authority from creator names.
"""
from __future__ import annotations

import json


def gateway(conn):
    from hermes_cli.kanban_db_sync import sync_state
    from hermes_cli.kanban_origin import gateway_names
    return gateway_names()[0] or (sync_state(conn) or {}).get("gateway_id") or ""


def released_upstream(conn, task_id, session) -> bool:
    """Refuse re-anchoring a chat released on this card or its creation source."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        return False
    ids = [task_id]
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='created' "
                       "ORDER BY id LIMIT 1", (task_id,)).fetchone()
    try:
        payload = json.loads(row[0] or "{}") if row else {}
    except (TypeError, ValueError):
        payload = {}
    for key in ("creator_task_id", "from_decompose_of"):
        if payload.get(key):
            ids.append(payload[key])
    return any(conn.execute("SELECT 1 FROM task_chat_links WHERE task_id=? AND chat_session=? AND released=1",
                            (tid, session)).fetchone() for tid in ids)
