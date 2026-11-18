"""Hold-only custody while an exact canonical stop request is unresolved."""
from __future__ import annotations

import json
import sqlite3


def pending(conn: sqlite3.Connection, task: str, known_stops: set[str]) -> bool:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='containment_delivery'").fetchone():
        return False
    from hermes_cli.kanban_db_sync import sync_state
    state = sync_state(conn)
    if state is None:
        return False
    rows = conn.execute("SELECT op_json FROM containment_delivery WHERE board=? AND gateway=? AND confirmed>=0",
                        (state["board_id"], state["gateway_id"]))
    for (raw,) in rows:
        try:
            op = json.loads(raw)
        except (ValueError, TypeError):
            return True  # corrupt request custody is not absence of a request
        if not isinstance(op, dict):
            return True
        if op.get("op") == "contain" and op.get("task") == task and op.get("event_uid") not in known_stops:
            return True
    return False
