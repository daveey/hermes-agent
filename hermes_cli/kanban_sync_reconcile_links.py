"""Reconciliation eligibility for chat links retained after a card leaves this gateway."""
from __future__ import annotations

import sqlite3


def existing_task_chat_links(conn: sqlite3.Connection, service_tasks: set[str]) -> set[str]:
    """Keep local-card links and remote-card links, not links with no surviving target."""
    local_tasks = {row[0] for row in conn.execute("SELECT id FROM tasks")}
    known = local_tasks | service_tasks
    return {uid for uid, task in conn.execute("SELECT uid, task_id FROM task_chat_links") if task in known}
