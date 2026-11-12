"""Field-scoped repair of uncaptured local writes."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from hermes_cli import kanban_db_sync as sync

_log = logging.getLogger(__name__)


def repair_divergence(live: sqlite3.Connection, shadow: sqlite3.Connection, gateway: str) -> dict[str, list[str]]:
    """Queue the local values of this gateway's cards that the service never received.

    A card whose live ``sync_version`` equals the replica's, with no outbox op in flight, has seen every service
    write. Any synced field that still differs was then written locally with no capture trigger, e.g. in the
    window while another process re-installed the triggers. The install heal only finds NULL-uid children;
    ``repair_missing_children`` handles settled missing links. A task row has no missing-row marker, so this
    field comparison is the only place that finds its uncaptured edits.
    Hub incident 2026-10-28: a ``scheduled -> ready`` wake reached the service as its event but not as its
    status, and the service refused every lease as ``not_claimable`` for 3 h. Returns {task id: fields queued}."""
    from hermes_cli.kanban_sync_client import _row_fields, _diverged_runs
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_sync_omissions import held_fields, repair_holds
    held = repair_holds(live, "tasks")
    seen = {r[0]: r[1] for r in shadow.execute(
        "SELECT id, sync_version FROM tasks WHERE (created_on = ? OR created_on IS NULL) AND sync_version IS NOT NULL",
        (gateway,))}
    repaired: dict[str, list[str]] = {}
    rows = live.execute("SELECT * FROM tasks WHERE sync_version IS NOT NULL").fetchall()
    for row in rows:
        tid = row["id"]
        if tid in held or seen.get(tid) != row["sync_version"]:
            continue
        theirs = shadow.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        mine, remote = _row_fields(live, "tasks", row), _row_fields(shadow, "tasks", theirs)
        omitted = held_fields(live, "tasks", tid)
        fields = {k: v for k, v in mine.items() if k in remote and remote[k] != v and k not in omitted}
        if not fields:
            continue
        if "status" in fields and _stale_lifecycle(live, shadow, row, theirs, mine.get("current_run_uid")):
            _adopt_replica(live, shadow, tid, theirs, row["status"])
            continue
        runs = _diverged_runs(live, shadow, tid)
        with write_txn(live):
            # [daveey] The same uncaptured transaction usually ended the card's run too (``complete_task`` writes
            # both). Queue those run rows FIRST: a card arriving ``done`` before its run makes the Board end the
            # lease's run generically (rows.ts ``endLeaseRun``: ``released``, "card left running (done)"), and
            # with no later holder write that row came back over the real ``done/completed`` run. The owner then
            # published the card unverified, and no origin ever released it (hub t_01a185075c59bd52, 10-29).
            for run_uid, run_fields, run_version in runs:
                live.execute(
                    "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, "
                    "created_at) VALUES (?, 'task_runs', ?, 'upsert', ?, ?, '', ?)",
                    (sync.uuid7(), run_uid, json.dumps(run_fields), None, int(time.time() * 1000)))
            live.execute(
                "INSERT INTO kanban_sync_outbox (op_id, table_name, row_uid, op, fields, base_version, hlc, created_at) "
                "VALUES (?, 'tasks', ?, 'upsert', ?, ?, '', ?)",
                (sync.uuid7(), tid, json.dumps(fields), row["sync_version"], int(time.time() * 1000)))
        repaired[tid] = sorted(fields) + [f"task_runs:{uid}" for uid, _f, _v in runs]
        _log.warning("kanban sync: %s %s differed from the service with nothing in flight (an uncaptured write); "
                     "re-queued", tid, ", ".join(repaired[tid]))
    return repaired


_TERMINAL = frozenset({"done", "archived"})


def _run_ended(conn: sqlite3.Connection, uid: str) -> bool:
    row = conn.execute("SELECT ended_at FROM task_runs WHERE uid = ?", (uid,)).fetchone()
    return row is not None and row[0] is not None


def _stale_lifecycle(live: sqlite3.Connection, shadow: sqlite3.Connection, row: sqlite3.Row, theirs: sqlite3.Row,
                     run_uid: str | None) -> bool:
    """True when the live status is older than the replica's, not a local edit the service missed.

    ``running`` names an open run: when that run has already ended (in either copy), no local write can have
    produced the live ``running``; the live row missed the end of its own run. A live non-terminal status over a
    replica ``done``/``archived`` card on the same run is the same gap. Pushing it would reopen the card for every
    reader, because the owner's HLC is newer than the worker's end (hub t_01a1ceaeb6ba593c, 2026-11-12). A live
    ``claim_lock`` is a local worker holding the card right now, so that copy is never stale. [daveey]"""
    if not run_uid:
        return False
    if row["status"] == "running" and (_run_ended(live, run_uid) or _run_ended(shadow, run_uid)):
        return True
    if row["claim_lock"] is not None:
        return False
    remote_run = shadow.execute("SELECT uid FROM task_runs WHERE id = ?", (theirs["current_run_id"],)).fetchone() \
        if theirs["current_run_id"] is not None else None
    return (theirs["status"] in _TERMINAL and row["status"] not in _TERMINAL
            and (remote_run is None or remote_run[0] == run_uid))


def _adopt_replica(live: sqlite3.Connection, shadow: sqlite3.Connection, tid: str, theirs: sqlite3.Row,
                   stale: str) -> None:
    """Apply the replica's copy of the card to the live file, as a feed apply (no capture, nothing sent)."""
    from hermes_cli.kanban_sync_client import _row_fields
    fields = _row_fields(shadow, "tasks", theirs)
    cursor = int((sync.sync_state(live) or {}).get("cursor") or 0)
    sync.apply_remote_changes(
        live, [sync.RemoteChange(seq=0, table="tasks", uid=tid, op="upsert", fields=fields,
                                 version=int(theirs["sync_version"]), hlc="", origin_gateway="")],
        new_cursor=cursor, observers=False, local_pending_wins=True)
    _log.warning("kanban sync: %s live status %r is older than the service's %r (its run already ended); "
                 "took the service's copy instead of re-sending it", tid, stale, theirs["status"])


# A run outcome that parks the card -> (its landing status, the event the park writes).
_PARKS = {"blocked": ("blocked", "blocked"), "review_requested": ("review", "review_requested"),
          "scheduled": ("scheduled", "scheduled")}
# Moves that take a card out of a park after its run ended; any later event naming a ``status`` counts too.
_LEFT_PARK = ("unblocked", "promoted", "promoted_manual", "reopened", "continuation_woken", "condition_met",
              "changes_requested", "dependency_wait", "block_loop_detected", "completed", "archived")


def _parked_status(live: sqlite3.Connection, tid: str, run: sqlite3.Row) -> str | None:
    """The park the ended run left the card in, or None when it is unknown or the card has left it since.

    A ``blocked`` outcome is not proof of ``blocked``: a dependency block lands ``todo`` and a loop ``triage``, so the
    park's own event (written in the run's end transaction, at or after the run's start) must name it, and no later
    event may have moved the card on (an unblock, a wake, a review reopen)."""
    park = _PARKS.get(run["outcome"] or "")
    if park is None:
        return None
    status, kind = park
    end = live.execute("SELECT id, created_at FROM task_events WHERE task_id = ? AND kind = ? "
                       "AND (run_id = ? OR created_at >= ?) ORDER BY created_at DESC, id DESC LIMIT 1",
                       (tid, kind, run["run_id"], run["started_at"])).fetchone()
    if end is None:
        return None
    marks = ",".join("?" * len(_LEFT_PARK))
    left = live.execute(
        f"SELECT 1 FROM task_events WHERE task_id = ? AND id != ? "
        f"AND (created_at > ? OR (created_at = ? AND id > ?)) "
        f"AND (kind IN ({marks}) OR json_extract(payload, '$.status') IS NOT NULL) LIMIT 1",
        (tid, end["id"], end["created_at"], end["created_at"], end["id"], *_LEFT_PARK)).fetchone()
    return None if left else status


def heal_running_over_ended_run(live: sqlite3.Connection) -> dict[str, str]:
    """Move this file's cards that read ``running`` with no claim, no lease and an ended current run to where that
    run left them; returns {task id: new status}.

    No writer produces that shape on purpose: a claim opens its run, a local worker holds ``claim_lock``, a pool
    worker holds the Board lease, and every end of a run moves the card in the same transaction. It is left by a
    stale copy sent over the run's end (hub t_01a1ceaeb6ba593c, 2026-11-12), and the dispatcher's stale-claim sweep
    never sees it (no claim to expire), so the card read ``running`` for good. A run ended ``completed`` with the
    card's latest ``completed`` event naming it is a completion: ``done``. A run that parked the card (``blocked``,
    ``review_requested``, ``scheduled``) puts it back in that park when the park's own event is the card's last
    lifecycle move (``_parked_status``), so a heal never re-dispatches a card that was waiting. Any other end is a
    reclaim: ``todo``, which the promoter turns into ``ready`` once the card's parents are done. The write goes
    through the capture triggers, so the Board's copy is corrected by the same round's push. [daveey]"""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn
    rows = live.execute(
        "SELECT t.id, r.id AS run_id, r.outcome, r.started_at, r.ended_at FROM tasks t "
        "JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.claim_lock IS NULL AND t.lease_holder IS NULL "
        "AND r.ended_at IS NOT NULL").fetchall()
    healed: dict[str, str] = {}
    for row in rows:
        tid, changed = row["id"], 0
        with write_txn(live):
            # ``run_id`` is the event column (synced as ``run_uid``); the payload key is the pre-column record.
            if live.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'status_healed' "
                            "AND (run_id = ? OR json_extract(payload, '$.run_id') = ?)",
                            (tid, row["run_id"], row["run_id"])).fetchone():
                _log.warning("kanban sync: %s still reads running over its ended run %s after one heal; the Board "
                             "kept a newer write, not healing it again", tid, row["run_id"])
                continue
            event = live.execute("SELECT run_id FROM task_events WHERE task_id = ? AND kind = 'completed' "
                                 "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
            done = row["outcome"] == "completed" and event is not None and event["run_id"] == row["run_id"]
            status = "done" if done else _parked_status(live, tid, row) or "todo"
            changed = live.execute(
                "UPDATE tasks SET status = ?, current_run_id = NULL, "
                "completed_at = CASE WHEN ? THEN COALESCE(completed_at, ?) ELSE NULL END, "
                "block_kind = CASE WHEN ? THEN NULL ELSE block_kind END "
                "WHERE id = ? AND status = 'running' AND claim_lock IS NULL AND lease_holder IS NULL "
                "AND current_run_id = ?", (status, done, row["ended_at"], done, tid, row["run_id"])).rowcount
            if changed:
                kb._append_event(live, tid, "status_healed", {"from": "running", "to": status,
                                                              "outcome": row["outcome"]}, run_id=row["run_id"])
        if changed:
            healed[tid] = status
            _log.warning("kanban sync: %s read running over its ended run %s (%s); moved to %s",
                         tid, row["run_id"], row["outcome"], status)
    return healed
