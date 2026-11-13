"""Card-scope source refusals block only AFTER the unused release is in the feed.

An ACK is insufficient: while the run is locally running, a status-leaving row
would race the release and retain custody on the refusing host.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync


ORPHAN_RETENTION_S = 7 * 24 * 3600
# A row whose follow-up keeps raising (bad refusal JSON, a block bug) is retried this many drains, then
# quarantined: its evidence moves to a task event and the row is dropped, so later feed applies stop logging it.
MAX_FOLLOWUP_FAILURES = 5
QUARANTINED_EVENT = "workspace_source_followup_quarantined"


def _ensure_schema(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS kanban_source_blocks ("
                 "run_uid TEXT PRIMARY KEY, task_id TEXT NOT NULL, refusal TEXT NOT NULL, "
                 "created_at INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(kanban_source_blocks)")}
    if "created_at" not in cols:
        conn.execute("ALTER TABLE kanban_source_blocks ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0")
        # Older PR-1 follow-ups get a full retention window, not an immediate expiry (once, with the ALTER).
        conn.execute("UPDATE kanban_source_blocks SET created_at=? WHERE created_at=0", (int(time.time()),))
    if "failures" not in cols:
        conn.execute("ALTER TABLE kanban_source_blocks ADD COLUMN failures INTEGER NOT NULL DEFAULT 0")


def _needs_migration(conn) -> bool:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(kanban_source_blocks)")}
    return not {"created_at", "failures"} <= cols


def defer(conn, claimed, exc) -> None:
    _ensure_schema(conn)
    conn.execute("INSERT OR IGNORE INTO kanban_source_blocks (run_uid,task_id,refusal,created_at) VALUES (?, ?, ?, ?)",
                 (sync.run_uid(conn, claimed.current_run_id), claimed.id,
                  json.dumps({"code": exc.code, "detail": exc.detail, "evidence": exc.evidence}), int(time.time())))


def drain(conn) -> None:
    """Best-effort post-commit work; never turn a committed feed into a failed apply.

    An empty (or absent) table opens no write transaction: every observed feed apply calls this."""
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='kanban_source_blocks'").fetchone():
            return
        if not conn.execute("SELECT 1 FROM kanban_source_blocks LIMIT 1").fetchone():
            return
        with kb.write_txn(conn):
            if _needs_migration(conn):
                _ensure_schema(conn)
            conn.execute(
                "DELETE FROM kanban_source_blocks WHERE created_at < ? AND ("
                "NOT EXISTS(SELECT 1 FROM tasks WHERE id=kanban_source_blocks.task_id) OR "
                "NOT EXISTS(SELECT 1 FROM task_runs WHERE uid=kanban_source_blocks.run_uid "
                "AND task_id=kanban_source_blocks.task_id))", (int(time.time()) - ORPHAN_RETENTION_S,),
            )
        uids = [r[0] for r in conn.execute("SELECT run_uid FROM kanban_source_blocks")]
    except sqlite3.Error:
        logging.getLogger(__name__).exception("source refusal follow-up scan failed")
        return
    for uid in uids:
        try:
            _drain_one(conn, uid)
        except Exception as exc:  # health: allow BLE001 -- post-commit work must not reject an already applied feed
            # Each block owns its transaction. Retain failed follow-ups for a bounded retry and keep draining
            # other cards; the feed cursor already committed.
            logging.getLogger(__name__).exception("source refusal follow-up failed for %s", uid)
            _count_failure(conn, uid, exc)


def _count_failure(conn, uid, exc) -> None:
    try:
        with kb.write_txn(conn):
            row = conn.execute("SELECT task_id, refusal, failures FROM kanban_source_blocks WHERE run_uid=?",
                               (uid,)).fetchone()
            if row is None:
                return
            failures = int(row["failures"] or 0) + 1
            if failures < MAX_FOLLOWUP_FAILURES:
                conn.execute("UPDATE kanban_source_blocks SET failures=? WHERE run_uid=?", (failures, uid))
                return
            if conn.execute("SELECT 1 FROM tasks WHERE id=?", (row["task_id"],)).fetchone():
                kb._append_event(conn, row["task_id"], QUARANTINED_EVENT, {
                    "run_uid": uid, "refusal": str(row["refusal"])[:2000], "failures": failures,
                    "error": f"{type(exc).__name__}: {exc}"[:500]})
            conn.execute("DELETE FROM kanban_source_blocks WHERE run_uid=?", (uid,))
    except sqlite3.Error:
        logging.getLogger(__name__).exception("source refusal follow-up failure count for %s failed", uid)


def _drain_one(conn, uid) -> None:
    row = conn.execute(
        "SELECT b.*, r.ended_at, r.outcome, r.metadata, t.status, t.current_run_id, t.lease_holder, "
        "r.id AS refused_run_id, (SELECT MAX(n.id) FROM task_runs n WHERE n.task_id=b.task_id) AS latest_run_id "
        "FROM kanban_source_blocks b JOIN task_runs r ON r.uid=b.run_uid AND r.task_id=b.task_id "
        "JOIN tasks t ON t.id=b.task_id WHERE b.run_uid=?", (uid,),
    ).fetchone()
    if row is None:
        return
    if row["ended_at"] is None or (row["current_run_id"] == row["refused_run_id"]
                                     and row["refused_run_id"] == row["latest_run_id"]):
        return  # task and run feed rows can arrive on separate pages, in either order
    # A different grant/ending won: discard, never block a newer run.
    valid = (row["refused_run_id"] == row["latest_run_id"] and row["outcome"] == "released"
             and json.loads(row["metadata"] or "{}").get("unused_grant") is True
             and row["current_run_id"] is None and not row["lease_holder"] and row["status"] in {"ready", "review"})
    if valid:
        refusal = json.loads(row["refusal"])
        from hermes_cli.kanban_workspace_source import WorkspaceSourceRefusal
        if row["status"] == "review":
            landed = _block_released_review(conn, row, WorkspaceSourceRefusal(**refusal))
        else:
            landed = block(conn, row["task_id"], WorkspaceSourceRefusal(**refusal))
        if not landed:
            return  # a refused follow-up is not a delivered human block
    with kb.write_txn(conn):
        conn.execute("DELETE FROM kanban_source_blocks WHERE run_uid=?", (uid,))


def _block_released_review(conn, row, exc) -> bool:
    """Only an unused, ended source-review grant may take this row-sync path.

    Board rowOp stores lifecycle rows (rows.ts), unlike TASK_OPS.block which
    deliberately excludes review. Do not widen the generic block_task API or
    re-open the released run. No lease means this write cannot change custody.
    """
    from hermes_cli.kanban_ask_resolved import store_request
    from hermes_cli.kanban_block_routing import latest_block_reason, route_block
    fields = _request(row["task_id"], exc)
    with kb.write_txn(conn):
        cur = conn.execute("SELECT block_kind, block_recurrences FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
        # Same-cause recurrence accounting as block_task: an unblock -> re-refuse loop reaches triage.
        new_status, event_kind, _sql, (kind, recurrences), payload = route_block(
            fields["kind"], fields["reason"], "review", prev_kind=cur["block_kind"] if cur else None,
            prev_recurrences=int((cur["block_recurrences"] if cur else 0) or 0),
            prev_reason=latest_block_reason(conn, row["task_id"]), compare_reason=True)
        changed = conn.execute(
            "UPDATE tasks SET status=?, block_kind=?, block_recurrences=?, "
            "claim_lock=NULL, claim_expires=NULL "
            "WHERE id=? AND status='review' AND current_run_id IS NULL AND lease_holder IS NULL "
            "AND ?=(SELECT MAX(id) FROM task_runs WHERE task_id=?)",
            (new_status, kind, recurrences, row["task_id"], row["refused_run_id"], row["task_id"]),
        ).rowcount
        if not changed:
            return False
        store_request(conn, row["task_id"], fields["human_request"])
        kb._append_event(conn, row["task_id"], event_kind, {
            **fields, **payload, "status": new_status, "workspace_source_refusal_run_uid": row["run_uid"],
        })
    return True


def block(conn, task_id, exc) -> bool:
    return kb.block_task(conn, task_id, **_request(task_id, exc))


def _request(task_id, exc) -> dict:
    evidence = "; ".join(f"{k}={v}" for k, v in exc.evidence.items()) or exc.detail or exc.code
    return dict(kind="needs_input", reason=(
        f"workspace_source {exc.code}: {exc.detail}. 1) Check the task branch on the remote and the custody "
        "holder's worktree. 2) Re-publish the source from its holder, then unblock this card."),
        human_request={"action": f"Restore the task branch ({exc.code}) and unblock this card. Unblock = retry "
                                 "materializing; archive = drop the card.",
                       "why_human": "the source moved or vanished outside a lease; only a person can decide "
                                    "which copy is right",
                       "interaction": "reply", "scope": f"card {task_id}", "evidence": evidence[:500]})
