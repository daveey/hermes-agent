"""Card-scope source refusals block only AFTER the unused release is in the feed.

An ACK is insufficient: while the run is locally running, a status-leaving row
would race the release and retain custody on the refusing host.
"""
from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync


def defer(conn, claimed, exc) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_source_blocks ("
                 "run_uid TEXT PRIMARY KEY, task_id TEXT NOT NULL, refusal TEXT NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO kanban_source_blocks VALUES (?, ?, ?)",
                 (sync.run_uid(conn, claimed.current_run_id), claimed.id,
                  json.dumps({"code": exc.code, "detail": exc.detail, "evidence": exc.evidence})))


def drain(conn) -> None:
    """Replay durable follow-ups after feed apply; superseded or lost releases do not block."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_source_blocks'").fetchone():
        return
    rows = conn.execute("SELECT b.*, r.ended_at, r.outcome, r.metadata, t.status, t.current_run_id, t.lease_holder, "
                        "r.id AS refused_run_id, (SELECT MAX(n.id) FROM task_runs n WHERE n.task_id=b.task_id) AS latest_run_id "
                        "FROM kanban_source_blocks b JOIN task_runs r ON r.uid=b.run_uid "
                        "JOIN tasks t ON t.id=b.task_id").fetchall()
    for row in rows:
        if row["ended_at"] is None or (row["current_run_id"] == row["refused_run_id"]
                                         and row["refused_run_id"] == row["latest_run_id"]):
            continue  # task and run feed rows can arrive on separate pages, in either order
        # A different grant/ending won: discard, never block a newer run.
        valid = (row["refused_run_id"] == row["latest_run_id"] and row["outcome"] == "released"
                 and json.loads(row["metadata"] or "{}").get("unused_grant") is True
                 and row["current_run_id"] is None and not row["lease_holder"] and row["status"] in {"ready", "review"})
        if valid:
            refusal = json.loads(row["refusal"])
            from hermes_cli.kanban_workspace_source import WorkspaceSourceRefusal
            block(conn, row["task_id"], WorkspaceSourceRefusal(**refusal))
        with kb.write_txn(conn):
            conn.execute("DELETE FROM kanban_source_blocks WHERE run_uid=?", (row["run_uid"],))


def block(conn, task_id, exc) -> bool:
    evidence = "; ".join(f"{k}={v}" for k, v in exc.evidence.items()) or exc.detail or exc.code
    return kb.block_task(conn, task_id, kind="needs_input", reason=(
        f"workspace_source {exc.code}: {exc.detail}. 1) Check the task branch on the remote and the custody "
        "holder's worktree. 2) Re-publish the source from its holder, then unblock this card."),
        human_request={"action": f"Restore the task branch ({exc.code}) and unblock this card. Unblock = retry "
                                 "materializing; archive = drop the card.",
                       "why_human": "the source moved or vanished outside a lease; only a person can decide "
                                    "which copy is right",
                       "interaction": "reply", "scope": f"card {task_id}", "evidence": evidence[:500]})
