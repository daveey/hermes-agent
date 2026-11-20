"""Release helpers for a second file of the same host: a non-primary live file and the owner notice key. [daveey]

``kanban_sync_quarantine_release`` releases the replica (shadow) first and then the live file. A non-primary live
file holds no feed rows of its own: :func:`replica_rows` takes the replica's current certified rows of every task
the release frees there, refuses while the replica still holds one (naming the receipt that holds it there and its
release command), and names this file's withheld rows of the freed tasks, which the lift deletes in the same
transaction (they are never replayed: the replica's certified rows replace them). :func:`first_seen` lets a primary
live file quarantine a newly uncertifiable event under the replica's first-seen stamp, so both files share one
owner-notice key (``kanban_sync_quarantine_notice.key``) and the host files ONE card. :func:`refused_suffix` is the
CLI's account of a release that stopped after an earlier file was released.
"""
from __future__ import annotations

import json

from hermes_cli.kanban_containment_lifecycle import ContainmentRefused


def first_seen(seen, uid: str) -> int | None:
    """The first-seen stamp (ms) ``seen`` (the replica) gave ``uid``'s quarantine, or None."""
    from hermes_cli import kanban_sync_quarantine as quarantine
    if seen is None or not quarantine._exists(seen):
        return None
    row = seen.execute("SELECT quarantined_at FROM containment_sync_quarantine WHERE uid=?", (uid,)).fetchone()
    return int(row[0]) if row else None


def _dropped(conn, freed: set[str], blocked: set[str]) -> list[str]:
    """This file's withheld wires of the freed tasks (none still held by another receipt)."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_quarantine as quarantine
    if not freed or not quarantine._exists(conn, "containment_sync_withheld"):
        return []
    out = []
    for (text,) in conn.execute("SELECT wire FROM containment_sync_withheld ORDER BY ord,rowid").fetchall():
        ch = sync.RemoteChange(**json.loads(text))
        try:
            names = quarantine.subjects(conn, ch.table, ch.uid, ch.fields)
        except ContainmentRefused:
            continue  # unidentifiable: stays withheld
        if names & freed and not names & blocked:
            out.append(text)
    return out


def replica_rows(conn, source, receipt: str, board: str, refused) -> tuple[list, list[str]]:
    """(catch-up changes, withheld wires to drop) for a non-primary live file: the replica's current rows of every
    freed task this file holds, and this file's withheld rows of the freed tasks. ``refused`` builds the error."""
    from hermes_cli import kanban_sync_quarantine as quarantine
    from hermes_cli.kanban_worker_node import _changes_for
    blocked = quarantine.tasks(conn, without=receipt)
    freed = quarantine.tasks(conn, receipt) - blocked
    present = [t for t in sorted(freed) if conn.execute("SELECT 1 FROM tasks WHERE id=?", (t,)).fetchone()]
    if present and source is None:
        raise refused("no replica to take the freed tasks' certified state from; the live hold stays")
    out = []
    for task in present:
        if quarantine.held(source, task):
            mine = quarantine.holders(source, task)
            raise refused(f"the replica still holds {task} under receipt {', '.join(mine)}: release it there first "
                          f"(`{quarantine.release_command(board, mine[0])}`), then run this release again")
        card = source.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
        if card is not None:
            out.extend(_changes_for(source, task, card))
    return out, _dropped(conn, freed, blocked)


def refused_suffix(slug: str, done: list[tuple[str, dict]]) -> str:
    """What an interrupted release already did, and what the operator runs next."""
    from hermes_cli.kanban_sync_quarantine import release_command
    if not done:
        return ""
    names = ", ".join(name for name, _ in done)
    newly = sorted({u for _, r in done for u in r["quarantined"]})
    if not newly:
        return f" (already released in: {names}; rerun to finish)"
    return (f" (already released in: {names}; its catch-up QUARANTINED "
            + "; ".join(f"{u} (release with `{release_command(slug, u)}`)" for u in newly)
            + "; release that first, then run this release again for the remaining file)")
