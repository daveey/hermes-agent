"""A run that reports its PR and parks in the same second hands that PR off. [daveey]

The ``active_pr`` guard orders the newest PR comment against handoff events by whole
seconds, and a tie stays guarded. An owner normally posts its checkpoint note naming the
PR and then parks (``kanban_schedule`` or ``kanban_continue``) in one turn, so the two
often share a second (2026-11-13 t_01a1d30035fa4777: comment 15615 and
its ``scheduled`` event both at 1794542252; held 30+ min after its awaited review approved).

The tie counts as "reported, then parked" only when every fact is proven:

* every PR comment at that second has exactly one ``commented`` event, written by the
  run that parked (portable ``writer_run_uid`` on a synced schema; the database-local
  integer only on an unsynced one);
* that run is the card's :func:`latest_parked_run` (the one "parked run" definition the
  awaited-review release shares): its latest executed run, of the assignee, that ENDED as
  that park, so a run that wrote its park event and then crashed or was reclaimed is not;
* its park event (the kind its outcome names) is in the same second, and each comment's
  event precedes it.

A comment written after the park in that second (a new PR named post-park), another run's
or a chat's comment, or unreadable provenance keeps today's hold.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

PARK_KINDS = ("scheduled", "continued")  # run outcome == the kind of its park event


def latest_parked_run(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    """The card's latest executed run when it ended as the assignee's orderly timed park, else None.

    The one definition every same-run handoff shares (this module and
    :mod:`kanban_awaited_review`): the latest run that executed (core-update requeues and
    unused grants skipped), run by the card's assignee, with ``outcome`` ``scheduled`` or
    ``continued`` and ``ended_at`` set. A park event alone is not a park: the run may have
    crashed, been reclaimed or timed out in the same second, or still be running. [daveey]"""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_core_update_requeue import latest_run_skipping_requeues

    run = latest_run_skipping_requeues(conn, task_id)
    task = kb.get_task(conn, task_id)
    if (run is None or task is None or run["profile"] != task.assignee
            or run["outcome"] not in PARK_KINDS or run["ended_at"] is None):
        return None
    return run


def _comment_event_ids(conn: sqlite3.Connection, task_id: str) -> dict[int, list[tuple[int, dict]]] | None:
    """Local comment row id -> [(commented event id, payload)]; None when any commented
    event is unreadable (it could be a second event of a tie comment)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch_pr_state import _comment_events, _comment_rows, _event_row_id

    if _comment_events(conn, task_id) is None:
        return None
    rows = _comment_rows(conn, task_id)
    out: dict[int, list[tuple[int, dict]]] = {}
    for eid, payload, created_at in conn.execute(
            "SELECT id, payload, created_at FROM task_events WHERE task_id = ? AND kind = 'commented'",
            (task_id,)):
        data = kb._json_dict(payload)
        cid = _event_row_id(rows, kb._to_epoch(created_at), data)
        if cid is not None:
            out.setdefault(cid, []).append((eid, data))
    return out


def reported_then_parked(conn: sqlite3.Connection, task_id: str, since: int,
                         tie_comment_ids: Iterable[int]) -> bool:
    """True when the PR comments at ``since`` and a later-ordered timed park share a run."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_pr_park_note import _writer_matches

    ids = set(tie_comment_ids)
    if not ids:
        return False
    run = latest_parked_run(conn, task_id)
    if run is None:
        return False
    parks = [e for e in conn.execute(
        "SELECT id, created_at FROM task_events WHERE task_id = ? AND run_id = ? AND kind = ?",
        (task_id, run["id"], run["outcome"])) if kb._to_epoch(e["created_at"]) == since]
    if not parks:
        return False
    park_id = max(e["id"] for e in parks)
    events = _comment_event_ids(conn, task_id)
    if events is None:
        return False
    for cid in ids:
        found = events.get(cid, [])
        if len(found) != 1:
            return False  # absent or ambiguous provenance fails closed
        eid, data = found[0]
        if eid >= park_id or not _writer_matches(conn, data, run["id"]):
            return False
    return True
