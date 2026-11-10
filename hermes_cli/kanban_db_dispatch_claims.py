"""Claim bookkeeping beside the dispatcher: the in-flight claim test and the leftover local claim sweep. [daveey]

Moved out of ``kanban_db_dispatch`` (which re-exports these names).
"""
from __future__ import annotations

import sqlite3
import time


def _kb():
    from hermes_cli import kanban_db
    return kanban_db


def _worker_alive(pid, started_at) -> bool:
    from hermes_cli.kanban_db_dispatch import _worker_alive as alive
    return alive(pid, started_at)


# A ``claimed`` event younger than this means a claim may still be publishing, so the orphan sweep leaves
# the card alone. One claim publication (``claim_task``/``claim_review_task``) is: the Board lease grant
# RPC (``kanban_sync_client.HTTP_TIMEOUT_S`` = 30 s), the claim txn's ``BEGIN IMMEDIATE`` (busy timeout,
# ``DEFAULT_BUSY_TIMEOUT_MS`` = 120 s), then, when the CAS was refused, the release RPC of the unused
# lease (30 s): 180 s worst case. The Board's grant row can reach the local DB anywhere in that window
# and leaves the card ``running`` with no ``claim_lock``. A real orphan waits at most this long more.
ORPHAN_CLAIM_GRACE_SECONDS = 180


def _claim_in_flight(conn: sqlite3.Connection, task_id: str, now: int) -> bool:
    """A claim on ``task_id`` may still be publishing: claimed within the grace, or the Board
    lease mirror says this gateway holds an unexpired lease on it."""
    if conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'claimed' AND created_at >= ? LIMIT 1",
        (task_id, now - ORPHAN_CLAIM_GRACE_SECONDS),
    ).fetchone():
        return True
    return _kb()._sync.held_by_us_live(conn, task_id, now)


_LEFTOVER_CLAIM_STATUSES = ("ready", "todo", "review", "blocked", "scheduled", "done")


def release_leftover_local_claims(conn: sqlite3.Connection) -> list[str]:
    """Clear this host's claim fields left on cards that are no longer ``running``; returns their ids. [daveey]

    Invariant: only a ``running`` card holds a local claim. A status-only write (the sync feed's Board lease-expiry
    row; claim columns are local and never overwritten by a pull) can move a card off ``running`` and leave the dead
    claimer's ``claim_lock``/``worker_pid`` behind. The dispatcher claims only ``claim_lock IS NULL`` cards and the
    other sweeps look at ``running`` only, so such a card never started and nothing said why (self-heal
    agent-dc4177df2e: T6 t_01a1a09e94bfaa25 sat ready 5 days on ``hermes-hub:28820``). Only host-local locks are
    judged (a pid means nothing elsewhere); a live worker, a claim in flight, or a card leased elsewhere is left
    alone. Status is unchanged; the CAS matches the exact values read."""
    now = int(time.time())
    prefix = _kb()._host_prefix()
    marks = ",".join("?" * len(_LEFTOVER_CLAIM_STATUSES))
    rows = conn.execute(
        f"SELECT id, status, claim_lock, claim_expires, worker_pid, worker_started_at FROM tasks "
        f"WHERE status IN ({marks}) AND claim_lock IS NOT NULL AND substr(claim_lock, 1, ?) = ?"
        + _kb()._sync.held_elsewhere_sql(conn),
        (*_LEFTOVER_CLAIM_STATUSES, len(prefix), prefix),
    ).fetchall()
    released: list[str] = []
    for row in rows:
        tid, pid = row["id"], row["worker_pid"]
        if pid and _worker_alive(pid, _kb()._row_get(row, "worker_started_at")):
            continue
        if _claim_in_flight(conn, tid, now):
            continue
        with _kb().write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
                "worker_started_at = NULL, last_heartbeat_at = NULL "
                "WHERE id = ? AND status = ? AND claim_lock IS ? AND claim_expires IS ? AND worker_pid IS ?",
                (tid, row["status"], row["claim_lock"], row["claim_expires"], pid),
            )
            if cur.rowcount != 1:
                continue
            _kb()._append_event(conn, tid, "claim_released", {
                "reason": "leftover_local_claim", "status": row["status"], "claim_lock": row["claim_lock"],
                "worker_pid": int(pid) if pid else None, "now": now,
            })
        released.append(tid)
    if released:
        _kb()._log.info("kanban: released leftover local claims on non-running cards: %s", released)
    return released
