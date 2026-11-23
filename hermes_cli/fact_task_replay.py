"""Indexed, bounded task dirty windows for private bootstrap replay. [daveey]"""
from dataclasses import replace

from hermes_cli import kanban_activity_revision as rev, kanban_task_change as changes
from hermes_cli.fact_capture import CaptureRefused


def coverage(conn, anchor):
    """Read lineage and the explicit FULL mark independently of dirty overflow."""
    _, fresh = changes.changed_since(conn, None)
    if fresh is None:
        raise CaptureRefused("bootstrap revision coverage lost")
    if anchor is not None:
        if replace(fresh, revision=anchor.revision) != anchor or fresh.revision < anchor.revision:
            raise CaptureRefused("bootstrap revision coverage lost")
        mark = conn.execute(f"SELECT rev FROM {rev.CHANGE_TABLE} WHERE task_id=?",
                            (rev.FULL_MARK,)).fetchone()
        if mark and mark[0] > anchor.revision:
            raise CaptureRefused("bootstrap revision coverage lost")
    return fresh


def page(conn, anchor, position, fence, limit):
    """A closed revision window; later mutations are replayed in the next window.

    Marks are latest-per-key, not an event log: a key moving beyond the fence
    disappears from this window and MUST be included in the subsequent window.
    Hydration is current to this transaction, so publish only after closure at
    the current head, never merely because the original window is exhausted.
    """
    fresh = coverage(conn, anchor)
    fence = fence or fresh
    if replace(fresh, revision=fence.revision) != fence or fresh.revision < fence.revision:
        raise CaptureRefused("bootstrap replay fence lost")
    # The existing rev index includes rowid as its tie-breaker. Reuse it rather
    # than requiring a writer/schema migration before a read-only bootstrap.
    position = position or (anchor.revision, 0)
    rows = tuple(conn.execute(
        f"SELECT rev, rowid, task_id FROM {rev.CHANGE_TABLE} INDEXED BY {rev.CHANGE_INDEX} "
        "WHERE (rev, rowid) > (?, ?) AND rev > ? AND rev <= ? ORDER BY rev, rowid LIMIT ?",
        (*position, anchor.revision, fence.revision, limit)))
    return tuple(row[2] for row in rows), fresh, fence, (rows[-1][:2] if rows else position)
