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
* a holding PR comment from before that claim (and after the card's first run) was handed to
  it: either an earlier requeued publisher of the same profile wrote it and every run from that
  writer up to the claim is skipped (this grant's own chain: that hand-over records no event),
  or a recorded handoff (:data:`_HANDOFF_KINDS`) falls after that comment, after every earlier
  real run ended, and no later than the claim. The claim itself records no proof that the
  respawn guard let it through, and a direct ``claim_task`` bypasses the guard, so without this
  an earlier crashed publisher's PR would ride along once the claimed run restated it;
* every run claimed after it is itself skipped (a core-update requeue or an attested unused
  grant): the next real execution consumes the grant, so its crash or timeout holds again.

Unknown, missing or ambiguous provenance, a second writer, another profile, an earlier PR with
no recorded handoff to the claim, or a later real run keeps the hold (fail closed). Chats and
known other-card runs are FYI to the guard already.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

# Events that hand an earlier PR to the next claim (the kinds ``_handed_off_after`` in
# :mod:`kanban_db_dispatch_pr_state` honours). :func:`_is_handoff` screens out link-only and
# dispatcher writes only; it does not repeat the guard's full per-kind predicates (merge
# authorization, PR-url scope, park/run match, review checkpoint), so it is the looser test.
_HANDOFF_KINDS = ("assigned", "changes_requested", "review_reopened", "unblocked", "continued", "scheduled",
                  "dependency_wait", "reopened", "hold_released", "operator_resumed")


def _has_run_uid(conn: sqlite3.Connection) -> bool:
    """True on a synced schema (``task_runs.uid``); probed once per :func:`continues` call."""
    try:
        conn.execute("SELECT uid FROM task_runs LIMIT 0")
    except sqlite3.OperationalError:
        return False
    return True


def _writer_run(conn: sqlite3.Connection, task_id: str, data: dict, synced: bool):
    """This card's run the ``commented`` payload names, or None when it can't be proven."""
    if not synced:
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


def _is_handoff(event) -> bool:
    """An owner park or operator act; never a link-only or dispatcher write."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch_rerun import is_handoff_event

    kind = event["kind"]
    data = kb._json_dict(event["payload"])
    if kind == "dependency_wait":
        return event["run_id"] is not None  # an owner's park closes its run; a bare link does not
    if kind == "reopened":
        return data.get("previous_status") == "done"
    if kind in ("hold_released", "operator_resumed"):
        via = "owner-wake-hold-release" if kind == "hold_released" else "operator-resume"
        return data.get("via") == via and data.get("scope") == "one_continuation"
    return is_handoff_event(kind, event["payload"])


def _requeued_chain(conn: sqlite3.Connection, task_id: str, writer, run, requeued: set[int]) -> bool:
    """``writer`` is an earlier requeued publisher whose one continuation reached ``run``'s claim.

    Same profile, a core-update requeue, and every run from it up to ``run`` skipped: the
    continuation it was granted is ``run``, and that hand-over records no event of its own.
    """
    from hermes_cli.kanban_core_update_requeue import _skipped

    if writer is None or writer["id"] not in requeued or writer["id"] >= run["id"]:
        return False
    if writer["profile"] != run["profile"]:
        return False
    between = conn.execute("SELECT * FROM task_runs WHERE task_id = ? AND id >= ? AND id < ?",
                           (task_id, writer["id"], run["id"]))
    return all(_skipped(conn, r) for r in between)


def _handed_to_claim(conn: sqlite3.Connection, task_id: str, run, started: int, newest_earlier: int) -> bool:
    """The holding PR comment at ``newest_earlier`` (before ``run``'s claim) was handed to that claim.

    A PR comment from before the card's first run is a handoff's input, not a publication.
    Otherwise a handoff event must fall strictly after that comment, no earlier than the end of
    every earlier real run (a real run consumes a handoff) and no later than the claim.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_core_update_requeue import _skipped

    starts = [kb._to_epoch(r[0]) for r in conn.execute("SELECT started_at FROM task_runs WHERE task_id = ?",
                                                        (task_id,))]
    readable = [t for t in starts if t is not None]
    if not readable or len(readable) != len(starts):
        return False  # an unreadable start could hide the run that published that comment
    if newest_earlier < min(readable):
        return True
    floor = newest_earlier + 1
    for prior in conn.execute("SELECT * FROM task_runs WHERE task_id = ? AND id < ?", (task_id, run["id"])):
        if _skipped(conn, prior):
            continue
        ended = kb._to_epoch(prior["ended_at"])
        if ended is None:
            return False  # an earlier real run with no readable end orders nothing after it
        floor = max(floor, ended)
    marks = ", ".join("?" * len(_HANDOFF_KINDS))
    for event in conn.execute(f"SELECT kind, payload, run_id, created_at FROM task_events "
                              f"WHERE task_id = ? AND kind IN ({marks})", (task_id, *_HANDOFF_KINDS)):
        at = kb._to_epoch(event["created_at"])
        if at is not None and floor <= at <= started and _is_handoff(event):
            return True
    return False


def continues(conn: sqlite3.Connection, task_id: str, holding: Iterable[tuple[int, int]],
              latest_pr_comment_id: int) -> bool:
    """True when the requeued publisher of the holding PR comments may continue once.

    ``holding``: the guard's holding PR comments as (epoch, local row id), excluding a park's
    own restated notes; ``latest_pr_comment_id`` is the newest of them.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_core_update_requeue import _skipped, requeued_run_ids

    requeued = requeued_run_ids(conn, task_id)
    if not requeued:
        return False  # the common held card: no core-update requeue, so no scan

    from hermes_cli.kanban_db_dispatch_pr_state import _comment_events

    events = _comment_events(conn, task_id)
    if events is None:
        return False  # an unreadable ``commented`` event: no row's writer can be proven
    by_row: dict[Optional[int], list[dict]] = {}
    for data, _at, cid in events:
        by_row.setdefault(cid, []).append(data)
    synced = _has_run_uid(conn)

    def writer(cid: int):
        datas = by_row.get(cid, [])
        return _writer_run(conn, task_id, datas[0], synced) if len(datas) == 1 else None

    run = writer(latest_pr_comment_id)
    if run is None or run["id"] not in requeued:
        return False
    task = kb.get_task(conn, task_id)
    if task is None or run["profile"] != task.assignee:
        return False
    started = kb._to_epoch(run["started_at"])
    if started is None:
        return False
    earlier: list[int] = []
    for t, cid in holding:
        if t < started:
            if not _requeued_chain(conn, task_id, writer(cid), run, requeued):
                earlier.append(t)  # not this grant's own chain: it needs a recorded handoff
            continue
        other = writer(cid)
        if other is None or other["id"] != run["id"]:
            return False  # a second (or unknown) publisher since the run was claimed
    if earlier and not _handed_to_claim(conn, task_id, run, started, max(earlier)):
        return False  # an earlier PR no handoff gave this claim (e.g. a direct claim_task)
    later = conn.execute("SELECT * FROM task_runs WHERE task_id = ? AND id > ?", (task_id, run["id"]))
    return all(_skipped(conn, r) for r in later)
