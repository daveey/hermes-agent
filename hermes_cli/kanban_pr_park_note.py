"""The parked run's own checkpoint note does not post-date its park. [daveey]

A worker that parks orderly (``scheduled`` timed wait or ``continued`` checkpoint) often
writes its checkpoint note a few seconds AFTER the park lands: the tool call ends the run,
then the agent posts its summary. When that note restates the card's PR, the respawn
guard's ``active_pr`` rule read it as fresh publication newer than the park, so the park no
longer counted as a handoff and the owner's wake was held (2026-11-12 MacBook
t_01a1cff0541bf30c: park 06:48:32, own note 06:48:39 naming PR #1486, wake held until an
operator ``kanban resume``).

Such a note is part of the park, not new evidence, but only when every fact is proven:

* its ``commented`` event names exactly the run that recorded the latest orderly park
  (the portable ``writer_run_uid`` on a synced schema; the database-local integer only on
  an unsynced one), with one event per comment row;
* it was written at or after the park and before any later claim of the card;
* every PR it names was already named in this card's own PR comments before the park.

A note naming a new PR, any other run's note, or unreadable provenance keeps today's rules.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from hermes_cli.kanban_db_dispatch_pr_policy import norm_pr

_PARK_KINDS = ("scheduled", "continued")


def _writer_matches(conn: sqlite3.Connection, data: dict, run_id: int) -> bool:
    try:
        row = conn.execute("SELECT uid FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    except sqlite3.OperationalError:
        # Unsynced schema: the integer is this database's own run id.
        writer = data.get("writer_run_id")
        return "writer_run_uid" not in data and type(writer) is int and writer == run_id
    uid = row[0]  # the caller proved the run row exists
    return isinstance(uid, str) and data.get("writer_run_uid") == uid


def restated_after_park(
    conn: sqlite3.Connection, task_id: str, candidates: list[tuple[int, int, list[str]]],
) -> tuple[set[int], int]:
    """(ids of ``candidates`` that are the parked run's own restatements, park epoch).

    ``candidates`` are the guard's own/unknown PR comments as (epoch, local row id, urls).
    Nothing proven: ``(set(), 0)``.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch_pr_state import _comment_events

    park = conn.execute(
        "SELECT id, run_id, created_at FROM task_events WHERE task_id = ? AND kind IN (?, ?) "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id, *_PARK_KINDS),
    ).fetchone()
    if park is None:
        return set(), 0
    park_id, run_id, parked_at = park["id"], park["run_id"], kb._to_epoch(park["created_at"])
    if parked_at is None:
        return set(), 0
    if conn.execute("SELECT 1 FROM task_runs WHERE id = ?", (run_id,)).fetchone() is None:
        return set(), 0  # a park naming no run row (or none) proves no writer
    claims = [(kb._to_epoch(r["created_at"]), r["id"]) for r in conn.execute(
        "SELECT id, created_at FROM task_events WHERE task_id = ? AND kind = 'claimed'", (task_id,))]
    later: list[int] = []
    for at, eid in claims:
        if at is None:
            return set(), 0  # an unreadable claim time cannot be ordered against the note
        if (at, eid) > (parked_at, park_id):
            later.append(at)
    bound = min(later, default=None)
    # Unreadable comment events (None) prove nothing; a row with two events is ambiguous below.
    by_row: dict[Optional[int], list[dict]] = {}
    for data, _at, cid in _comment_events(conn, task_id) or []:
        by_row.setdefault(cid, []).append(data)
    known = {norm_pr(u) for t, _cid, urls in candidates if t < parked_at for u in urls}
    restated: set[int] = set()
    for t, cid, urls in candidates:
        if t < parked_at or (bound is not None and t >= bound):
            continue
        datas = by_row.get(cid, [])
        if len(datas) != 1 or not _writer_matches(conn, datas[0], run_id):
            continue
        if all(norm_pr(u) in known for u in urls):
            restated.add(cid)
    return restated, parked_at
