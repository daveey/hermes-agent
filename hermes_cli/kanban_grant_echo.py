"""[daveey] The Board echo of a claim's OWN, still-unused lease grant (review F5).

``claim_gate`` acquires the Board lease over the network before ``claim_transaction`` takes the write txn and
``admission.lock``. The Board's ``grantLease`` writes the card ``running`` with the new run uid and
``lease_holder`` = this gateway, so the sync pull can apply that echo in between: the local row is then
``running`` / ``claim_lock IS NULL`` / own holder. Generic occupancy (``machine_metrics.active_card_runs``) rightly
counts such a row (it may be a stale or in-flight local run), but for the ONE claim the grant belongs to it is not
another run: counting it made six real runs read 7/7, and its ``running`` status made the claim's CAS lose.

:func:`own_grant_echo` proves, inside the claim's write transaction, that the row is exactly this grant's echo; the
final gate then excludes that single row from the whole-host count and the CAS adopts it. Every predicate must hold
or nothing is excluded (fail closed): unconsumed gate with a granted lease, primary board, file-backed DB identity,
row ``running`` with no claim lock, holder = this gateway = the gate's gateway, an unexpired lease deadline, and a
current run whose uid is the granted run uid, unlocked and not ended.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import NamedTuple, Optional


class GrantEcho(NamedTuple):
    db_identity: tuple[int, int]  # (st_dev, st_ino) of the claim connection's main database file
    task_id: str
    run_uid: str
    gateway_id: str
    run_id: int  # local task_runs.id the echo created (placeholder or mirrored run row)


# Rows of ``tasks t`` (joined to the current run ``r``) that are exactly the echo of grant (task, holder, run uid).
# Shared by the claim-side proof and the counter's re-check on its own read connection.
ECHO_MATCH_SQL = (
    "SELECT t.current_run_id FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
    "WHERE t.id = ? AND t.status = 'running' AND t.claim_lock IS NULL AND t.lease_holder = ? "
    "AND t.lease_expires IS NOT NULL AND t.lease_expires > ? "
    "AND r.uid = ? AND r.task_id = t.id AND r.claim_lock IS NULL AND r.ended_at IS NULL "
    "AND r.status IN ('pending_sync', 'running')"
)


def connection_db_identity(conn: sqlite3.Connection) -> Optional[tuple[int, int]]:
    """``(st_dev, st_ino)`` of the connection's main database file; None for memory/temp DBs or a stat failure."""
    try:
        row = next((r for r in conn.execute("PRAGMA database_list") if r[1] == "main"), None)
    except sqlite3.Error:
        return None
    path = row[2] if row is not None else ""
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def match_rows(conn: sqlite3.Connection, echo: GrantEcho) -> list[int]:
    now_ms = int(time.time() * 1000)
    return [r[0] for r in conn.execute(ECHO_MATCH_SQL, (echo.task_id, echo.gateway_id, now_ms, echo.run_uid))]


def own_grant_echo(conn: sqlite3.Connection, task_id: str, gate) -> Optional[GrantEcho]:
    """Inside the claim's write txn: the proven echo of ``gate``'s own unused grant, else None (nothing excluded)."""
    from hermes_cli import kanban_db_sync as sync

    lease = getattr(gate, "lease", None)
    ids = getattr(gate, "_ids", None)
    if lease is None or getattr(gate, "consumed", True) or not ids or not lease.granted:
        return None
    if sync.board_mode(conn) != "primary":
        return None
    try:
        own = sync._state(conn)["gateway_id"]
    except (sqlite3.Error, TypeError, KeyError, IndexError):
        return None
    if not own or own != ids[1] or ids[2] != task_id:
        return None
    identity = connection_db_identity(conn)
    if identity is None:
        return None
    probe = GrantEcho(identity, task_id, lease.run_uid, own, -1)
    matched = match_rows(conn, probe)
    if not matched:  # t.id is the key and the join is on its current run: at most one row
        return None
    return probe._replace(run_id=int(matched[0]))
