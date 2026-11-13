"""Dispatcher-only supervisor request gate; incoming operator traffic is independent. [daveey]

Active requests never skip: local holders can die without a database write and forwarded
requests read external courier/Board results. On empty queues, only task/request changes
or the executor's persisted liveness deadline require another local drain.
"""
from __future__ import annotations

import sqlite3

from hermes_cli import kanban_sweep_gate as gate


def _requests(conn: sqlite3.Connection) -> tuple:
    # These tables are not in the task activity revision. Include updates and deletions,
    # not just MAX(id); terminal rows still waiting for their requester matter too.
    return tuple(tuple(row) for row in conn.execute(
        "SELECT * FROM supervisor_requests WHERE status IN ('pending','accepted') "
        "OR requester_woken=0 ORDER BY id"))


def _key(conn: sqlite3.Connection) -> tuple:
    return gate.config_key(), _requests(conn)


def _due(conn: sqlite3.Connection) -> float:
    from hermes_cli import kanban_supervisor_requests as sr
    if conn.execute("SELECT 1 FROM supervisor_requests WHERE status IN ('pending','accepted') LIMIT 1").fetchone():
        return 0  # process death, external replies, pending backlog, and expiry: next tick
    row = conn.execute("SELECT last_seen FROM supervisor_executors WHERE executor_id=?",
                       (sr.executor_identity("dispatcher"),)).fetchone()
    # Preserve drain's strict integer-second comparison, not a new arbitrary cadence.
    return int(row[0]) + 61 if row else 0


def _enabled(_conn: sqlite3.Connection) -> bool:
    from hermes_cli.config import load_config_readonly
    cfg = ((load_config_readonly() or {}).get("kanban") or {}).get("supervisor_requests") or {}
    return cfg.get("enabled", True) is not False


def dispatch(conn: sqlite3.Connection, board: str | None) -> list:
    from hermes_cli import kanban_operator_call as kox
    from hermes_cli import kanban_supervisor_requests as sr
    if not gate.memo("supervisor.enabled", conn, _enabled, extra=gate.config_key):
        return []
    result = gate.gated("dispatch.supervisor_local", conn, sr.drain, conn,
                        board=board, role="dispatcher", incoming=False,
                        extra=_key, due=_due, skipped={"processed": []})
    # Replica calls, retry holds and label-process completions must not be hidden by
    # a local-board/request gate. They retain their own scanner and watermark.
    kox.apply_incoming(conn)
    return result["processed"]
