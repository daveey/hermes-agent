"""Local durable outbox for a single immutable caused-stop decision. [daveey]

This is not sync authority: only authenticated Board receipt readback settles it.
Keep completed requests too, so no later invocation can replace their bytes.
"""
from __future__ import annotations

import json

TABLE = "spend_review_requests"


def exists(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def requests(conn):
    if not exists(conn):
        return []
    return [json.loads(row[0]) for row in conn.execute(f"SELECT request FROM {TABLE}")]


def get(conn, task, episode):
    if not exists(conn):
        return None
    row = conn.execute(f"SELECT request FROM {TABLE} WHERE task=? AND episode=?", (task, episode)).fetchone()
    return json.loads(row[0]) if row else None


def freeze(conn, board, gateway, wire):
    from hermes_cli import kanban_db as kb
    # Network submission must never precede the outermost durable COMMIT.
    if conn.in_transaction:
        raise ValueError("cannot freeze a review inside another transaction")
    with kb.write_txn(conn):
        conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (task TEXT NOT NULL, episode TEXT NOT NULL, "
                     "request TEXT NOT NULL, PRIMARY KEY(task,episode))")
        request = {"board": board, "gateway": gateway, "wire": wire}
        conn.execute(f"INSERT OR IGNORE INTO {TABLE} VALUES (?,?,?)",
                     (wire["task"], wire["episode"], json.dumps(request, ensure_ascii=False)))
        frozen = get(conn, wire["task"], wire["episode"])
        assert frozen is not None
        return frozen


def refuse(conn, task, episode, reason):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        request = get(conn, task, episode)
        assert request is not None
        request["refused"] = reason
        conn.execute(f"UPDATE {TABLE} SET request=? WHERE task=? AND episode=?",
                     (json.dumps(request, ensure_ascii=False), task, episode))
