"""Admitted worker identity shared by CLI and agent containment adapters.

Identity comes from dispatcher pins plus durable claim/session adoption, never
from containment arguments or the informational WORKER_TASK shell variable.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from hermes_cli.kanban_containment_lifecycle import ContainmentRefused, _actor


@dataclass(frozen=True)
class Actor:
    task: str
    run_uid: str


def current_actor(conn: sqlite3.Connection) -> Actor:
    from agent.delegation_context import owned_kanban_task
    from gateway.session_context import get_session_env
    task = owned_kanban_task()
    pinned_db = os.environ.get("HERMES_KANBAN_DB")
    lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    session = get_session_env("HERMES_SESSION_ID")
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if (not task or not pinned_db or not path or Path(path).resolve() != Path(pinned_db).resolve()
            or not lock or not run_id or not run_id.isascii() or not run_id.isdecimal() or not session):
        raise ContainmentRefused("containment requires the dispatcher-admitted task, board, run, lock and session")
    row = conn.execute("SELECT id,claim_lock FROM task_runs WHERE task_id=? AND id=?", (task, int(run_id))).fetchone()
    if row is None or row[1] != lock:
        raise ContainmentRefused("containment caller does not own the pinned run claim")

    # Session adoption is recorded by the owner before agent execution. A leaked
    # environment from another conversation cannot borrow the current claim.
    adopted = False
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                               "AND kind='owner_admitted'", (task, int(run_id))):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("session_id") == session:
            adopted = True
            break
    if not adopted:
        raise ContainmentRefused("containment caller lacks the owning session adoption receipt")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
    uid = conn.execute("SELECT uid FROM task_runs WHERE id=?", (int(run_id),)).fetchone()[0] if "uid" in columns else None
    if uid is None:
        from hermes_cli.kanban_containment_connections import bootstrap
        bootstrap(conn)  # local-only; durable synced state never manufactures a lease UID
        uid = conn.execute("SELECT uid FROM task_runs WHERE id=?", (int(run_id),)).fetchone()[0]
    _actor(conn, task, uid)
    return Actor(task, uid)
