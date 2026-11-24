"""Certify closures older clients consumed as ordinary history, without releasing custody.

A fresh feed cursor cannot repair an absent local proof cache entry: the closure
will never be fetched again. Only triage rows with an existing closure are candidates;
canonical transport and the normal protected apply still decide their disposition.
"""
from __future__ import annotations

from hermes_cli import kanban_db_sync as sync

BATCH = 20


def backfill(conn, hub, board: str) -> dict[str, str]:
    """Bound one round's work; no scans of closed inventory and no local row as authority."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='containment_canonical_receipts'").fetchone():
        return {}
    from hermes_cli import kanban_sync_quarantine as quarantine
    from hermes_cli.kanban_containment_terminal import closed
    rows = conn.execute(
        "SELECT e.uid,e.task_id,e.kind,e.payload,e.origin_gateway FROM tasks t "
        "CROSS JOIN task_events e ON e.task_id=t.id WHERE t.status='triage' "
        "AND t.containment_episode IS NOT NULL AND e.kind='containment_closed' "
        "ORDER BY t.id,e.uid").fetchall()
    blocked = quarantine.tasks(conn)
    changes = []
    for row in rows:
        if row["task_id"] in blocked:
            continue
        cached = conn.execute("SELECT 1 FROM containment_canonical_receipts WHERE uid=?", (row["uid"],)).fetchone()
        # A crash can commit certification before projection. Retry that projection,
        # but do not re-fetch old closures on a genuinely later, still-held episode.
        if cached and not closed(conn, row["task_id"]):
            continue
        changes.append(sync.RemoteChange(0, "task_events", row["uid"], "upsert", dict(row), 0, "",
                                         row["origin_gateway"] or ""))
        if len(changes) == BATCH:
            break
    if not changes:
        return {}
    # Certified against fresh enrolled-Board evidence, including stop/closure pair.
    # Deterministic conflicts use the ordinary durable quarantine, never an override.
    changes = quarantine.certified_page(conn, hub, board, changes)
    cursor = int(sync.sync_state(conn)["cursor"])
    sync.apply_remote_changes(conn, changes, new_cursor=cursor, board=board, observers=False)
    # restore_page changes the projection after the ordinary observer collection.
    return {ch.fields["task_id"]: conn.execute("SELECT status FROM tasks WHERE id=?",
                                             (ch.fields["task_id"],)).fetchone()[0] for ch in changes}
