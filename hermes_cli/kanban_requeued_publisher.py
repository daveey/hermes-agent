"""A PR reported by a run the core updater then requeued gets one continuation. [daveey]

A hermes-sync core-update requeue is a pause (:mod:`kanban_core_update_requeue`). A run caught
between reporting its own PR and parking was then held ``active_pr`` with no worker: its PR
comment post-dates every handoff, and only an operator ``kanban resume`` lifted it (2026-11-13,
t_01a1d1d7998cda2a: run 20713 reported PR #1783, requeued 9 min later).

The pause lets that run's lineage continue exactly once, and only when every fact is proven:

* every holding PR comment written since that run was claimed, the newest included, was
  written by it: one ``commented`` event per row naming it (the portable ``writer_run_uid``
  on a synced schema, the database-local integer only on an unsynced one);
* that run ended as a core-update requeue (no crash, timeout or other reclaim);
* its profile is the card's current assignee;
* every run claimed after it is itself skipped (a core-update requeue or an attested unused
  grant): the next real execution consumes the grant, so its crash or timeout holds again.

Unknown, missing or ambiguous provenance, a second writer, another profile or a later real run
keeps the hold (fail closed). Chats and known other-card runs are FYI to the guard already.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Optional


def _writer_run(conn: sqlite3.Connection, task_id: str, data: dict):
    """This card's run the ``commented`` payload names, or None when it can't be proven."""
    try:
        conn.execute("SELECT uid FROM task_runs LIMIT 0")
    except sqlite3.OperationalError:
        # Unsynced schema: the integer is this database's own run id.
        writer = data.get("writer_run_id")
        if "writer_run_uid" in data or type(writer) is not int:
            return None
        run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (writer,)).fetchone()
    else:
        uid = data.get("writer_run_uid")
        if not isinstance(uid, str) or not uid:
            return None  # never fall back to the integer, which a replica may remap
        run = conn.execute("SELECT * FROM task_runs WHERE uid = ?", (uid,)).fetchone()
    return run if run is not None and run["task_id"] == task_id else None


def continues(conn: sqlite3.Connection, task_id: str, holding: Iterable[tuple[int, int]],
              latest_pr_comment_id: int) -> bool:
    """True when the requeued publisher of the holding PR comments may continue once.

    ``holding``: the guard's holding PR comments as (epoch, local row id), excluding a park's
    own restated notes; ``latest_pr_comment_id`` is the newest of them.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_core_update_requeue import _skipped, is_core_update_requeue
    from hermes_cli.kanban_db_dispatch_pr_state import _comment_events

    events = _comment_events(conn, task_id)
    if events is None:
        return False  # an unreadable ``commented`` event: no row's writer can be proven
    by_row: dict[Optional[int], list[dict]] = {}
    for data, _at, cid in events:
        by_row.setdefault(cid, []).append(data)

    def writer(cid: int):
        datas = by_row.get(cid, [])
        return _writer_run(conn, task_id, datas[0]) if len(datas) == 1 else None

    run = writer(latest_pr_comment_id)
    if run is None or not is_core_update_requeue(conn, run):
        return False
    task = kb.get_task(conn, task_id)
    if task is None or run["profile"] != task.assignee:
        return False
    started = kb._to_epoch(run["started_at"])
    if started is None:
        return False
    for t, cid in holding:
        if t >= started:
            other = writer(cid)
            if other is None or other["id"] != run["id"]:
                return False  # a second (or unknown) publisher since the run was claimed
    later = conn.execute("SELECT * FROM task_runs WHERE task_id = ? AND id > ?", (task_id, run["id"]))
    return all(_skipped(conn, r) for r in later)
