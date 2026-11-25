"""Retire explicitly abandoned scratch fixtures rather than re-dispatching them."""
from __future__ import annotations

import re


def eligible(conn, tid: str) -> bool:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_archive_scope import _creator_task_id, archive_scope_for
    task = kb.get_task(conn, tid)
    if not task or task.status != "triage" or task.workspace_kind != "scratch" or task.human_request:
        return False
    creator = _creator_task_id(conn, tid)
    if not creator or archive_scope_for(conn, tid, creator)[0]:
        return False
    parent = kb.get_task(conn, creator)
    if parent is None:
        from hermes_cli.kanban_foreign_card import _live_db_file, open_replica
        with open_replica(_live_db_file(conn)) as rep:
            parent = kb.get_task(rep, creator) if rep is not None else None
    if parent is None or parent.status not in ("done", "archived"):
        return False
    if conn.execute("SELECT 1 FROM task_links l JOIN tasks t ON t.id=l.child_id "
                    "WHERE l.parent_id=? AND t.status NOT IN ('done','archived') LIMIT 1", (tid,)).fetchone():
        return False
    latest = conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id DESC LIMIT 1",
                          (tid,)).fetchone()
    # Deliberately not a keyword classifier: historical requests provide an explicit
    # affirmative command. Negations, quotes of errors and vague mentions do not qualify.
    return bool(latest and re.search(
        rf"(?:^|[.!?]\s+)(?:Please )?[Rr]un `hermes kanban archive {re.escape(tid)}`(?:[ .]|$)",
        latest[0] or ""))


def archive_requested_fixture(conn, tid: str) -> bool:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_wake_check import foreign_ids
    if tid in foreign_ids(conn) or not eligible(conn, tid):
        return False
    def precondition(c):
        from hermes_cli.kanban_home_transfer import frozen
        if frozen(c, tid):
            return "home transfer owns this card; scratch cleanup deferred"
        if not eligible(c, tid):
            return "scratch cleanup conditions changed"
        kb.add_comment(c, tid, "triage-sweep", "Archived scratch card of a finished creator: the latest comment "
                       "explicitly requested its archive. It will not be dispatched again.")
        return None
    return kb.archive_task(conn, tid, via="triage scratch cleanup", precondition=precondition)
