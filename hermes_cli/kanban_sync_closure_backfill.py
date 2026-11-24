"""Certify closures older clients consumed as ordinary history, without releasing custody.

A fresh feed cursor cannot repair an absent local proof cache entry: the closure
will never be fetched again. Only triage rows with an existing closure are candidates;
canonical transport and the normal protected apply still decide their disposition.
"""
from __future__ import annotations

import logging
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused

BATCH = 20
RETRY_S = 900
_clock = time.time
log = logging.getLogger(__name__)


def backfill(conn, hub, board: str) -> dict[str, str]:
    """Bound one round's work; no local row or retry ledger is disposition authority."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='containment_canonical_receipts'").fetchone():
        return {}
    from hermes_cli import kanban_sync_quarantine as quarantine
    from hermes_cli.kanban_containment_terminal import closed
    # Host-local retry scheduling only. No hold is lifted and no proof is invented.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='containment_closure_backfill_retry'").fetchone():
        with kb.write_txn(conn, allow_nested=True):
            conn.execute("CREATE TABLE IF NOT EXISTS containment_closure_backfill_retry "
                         "(uid TEXT PRIMARY KEY,retry_at INTEGER NOT NULL)")
    now = int(_clock())
    rows = conn.execute(
        "SELECT e.uid,e.task_id,e.kind,e.payload,e.origin_gateway FROM tasks t "
        "CROSS JOIN task_events e ON e.task_id=t.id WHERE t.status='triage' "
        "AND t.containment_episode IS NOT NULL AND e.kind='containment_closed' "
        "AND NOT EXISTS (SELECT 1 FROM containment_closure_backfill_retry r WHERE r.uid=e.uid AND r.retry_at>?) "
        "ORDER BY t.id,e.uid", (now,)).fetchall()
    blocked = quarantine.tasks(conn)
    changes = []
    for row in rows:
        if row["task_id"] in blocked:
            continue
        cached = conn.execute("SELECT 1 FROM containment_canonical_receipts WHERE uid=?", (row["uid"],)).fetchone()
        # Retry a cache-committed/projection-crash, not a later still-held episode.
        if cached and not closed(conn, row["task_id"]):
            continue
        changes.append(sync.RemoteChange(0, "task_events", row["uid"], "upsert", dict(row), 0, "",
                                         row["origin_gateway"] or ""))
        if len(changes) == BATCH:
            break
    before = {ch.fields["task_id"]: "triage" for ch in changes}
    certified = []
    for change in changes:
        try:
            certified.extend(quarantine.certified_page(conn, hub, board, [change]))
        except ContainmentRefused as exc:
            # This history is already behind the cursor: refusing its optional
            # repair must not stop fresh feed, attachments or unrelated repairs.
            # Retry boundedly, without converting transient missing proof to a hold.
            with kb.write_txn(conn, allow_nested=True):
                conn.execute("INSERT OR REPLACE INTO containment_closure_backfill_retry VALUES (?,?)",
                             (change.uid, now + RETRY_S))
            log.warning("kanban sync: historical closure %s backfill deferred: %s", change.uid, exc)
    if not certified:
        return {}
    cursor = int(sync.sync_state(conn)["cursor"])
    sync.apply_remote_changes(conn, certified, new_cursor=cursor, board=board, observers=False)
    after = {ch.fields["task_id"]: conn.execute("SELECT status FROM tasks WHERE id=?",
                                              (ch.fields["task_id"],)).fetchone()[0] for ch in certified}
    return {task: status for task, status in after.items() if status != before[task]}
