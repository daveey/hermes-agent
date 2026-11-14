"""Field-scoped repair of uncaptured local writes."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_shared_blocker_fence import consistent_uid_milliseconds

_log = logging.getLogger(__name__)
_CHUNK = 500


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
    from hermes_cli.kanban_sync_omissions import repair_holds
    held = repair_holds(live, "tasks")
    repaired: dict[str, list[str]] = {}
    candidates = _differing_settled(live, shadow, gateway)
    if candidates is None:  # cannot pre-filter in SQLite (in-memory/unknown files, an error): scan every card
        seen = {r[0]: r[1] for r in shadow.execute(
            "SELECT id, sync_version FROM tasks WHERE (created_on = ? OR created_on IS NULL) "
            "AND sync_version IS NOT NULL", (gateway,))}
        rows = [r for r in live.execute("SELECT * FROM tasks WHERE sync_version IS NOT NULL").fetchall()
                if r["id"] not in held and seen.get(r["id"]) == r["sync_version"]]
    else:
        # Only the cards whose raw columns differ come back to Python; the replica row read below re-checks the
        # settled condition (version, creator), since either file may have moved since the pre-filter read.
        ids = sorted(candidates - held)
        rows = []
        for index in range(0, len(ids), _CHUNK):
            part = ids[index:index + _CHUNK]
            rows += live.execute(f"SELECT * FROM tasks WHERE sync_version IS NOT NULL AND id IN "
                                 f"({','.join('?' * len(part))}) ORDER BY rowid", part).fetchall()
    # One replica read per chunk and one omissions read per pass, not one of each per settled card: the scan
    # visits every synced card each round (13% of hub gateway CPU at 4,948 cards, 2026-11-12). [daveey]
    omissions: dict[str, set[str]] = {}
    for uid, field in live.execute(
            "SELECT row_uid, field FROM kanban_sync_omissions WHERE table_name='tasks' AND state!='delivered'"):
        omissions.setdefault(uid, set()).add(field)
    replica: dict[str, sqlite3.Row] = {}
    for index, row in enumerate(rows):
        if index % _CHUNK == 0:
            ids = [r["id"] for r in rows[index:index + _CHUNK]]
            replica = {r["id"]: r for r in shadow.execute(
                f"SELECT * FROM tasks WHERE id IN ({','.join('?' * len(ids))})", ids)}
        tid = row["id"]
        theirs = replica.get(tid)
        if theirs is None:  # left the replica since ``seen`` was read: nothing to compare this pass
            continue
        if candidates is not None and (theirs["sync_version"] != row["sync_version"]
                                       or theirs["created_on"] not in (gateway, None)):
            continue  # not settled (or not this gateway's card) as of this read
        mine, remote = _row_fields(live, "tasks", row), _row_fields(shadow, "tasks", theirs)
        omitted = omissions.get(tid, ())
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


def _db_file(conn: sqlite3.Connection) -> str | None:
    """The main database file of ``conn``; None for an in-memory or temporary database."""
    for _seq, name, file in conn.execute("PRAGMA database_list").fetchall():
        if name == "main":
            return file or None
    return None


def _task_columns(conn: sqlite3.Connection, schema: str = "main") -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA {schema}.table_info(tasks)").fetchall()}


def _differing_settled(live: sqlite3.Connection, shadow: sqlite3.Connection, gateway: str) -> set[str] | None:
    """Ids of this gateway's settled cards (same ``sync_version`` in both copies) whose synced columns differ.

    One SQLite join over both files, in a private read-only connection: the comparison runs in C and only the
    differing ids cross into Python. Scanning every settled card in Python made each round fetch every task row
    twice (``SELECT *``, ~5.5k rows on the hub); under the gateway's GIL contention each fetched row waits for the
    GIL, so the scan held the sync loop 40+ s per pass and the replica went 11+ min stale (hub py-spy 04:17Z
    2026-11-13, t_01a1d2cc49634a78: 100% of the sync thread in ``repair_divergence``). Equal raw values always
    give equal ``_row_fields`` values, so this is a superset of the cards the Python comparison flags; the caller
    re-compares each candidate exactly. None when the pre-filter cannot run: the caller scans every card. [daveey]"""
    from hermes_cli import kanban_lockguard
    live_file, replica_file = _db_file(live), _db_file(shadow)
    if not live_file or not replica_file:
        return None
    try:
        conn = kanban_lockguard.connect(Path(live_file).resolve().as_uri() + "?mode=ro", uri=True)
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        conn.execute("ATTACH DATABASE ? AS rep", (Path(replica_file).resolve().as_uri() + "?mode=ro",))
        skip = sync.LOCAL_COLUMNS["tasks"] | sync.KEY_COLUMNS["tasks"] | sync.SERVER_COLUMNS.get("tasks", frozenset())
        columns = sorted((sync.SYNCED_COLUMNS["tasks"] - skip) & _task_columns(conn) & _task_columns(conn, "rep"))
        terms = []
        for column in columns:
            if ("tasks", column) in sync.REF_COLUMNS:  # local run ids differ per file: compare the run uids
                terms.append(f"(SELECT uid FROM main.task_runs WHERE id = l.{column}) IS NOT "
                             f"(SELECT uid FROM rep.task_runs WHERE id = r.{column})")
            else:
                terms.append(f"l.{column} IS NOT r.{column}")
        if not terms:
            return set()
        return {row[0] for row in conn.execute(
            "SELECT l.id FROM main.tasks l JOIN rep.tasks r ON r.id = l.id "
            "WHERE l.sync_version IS NOT NULL AND r.sync_version = l.sync_version "
            "AND (r.created_on = ? OR r.created_on IS NULL) AND (" + " OR ".join(terms) + ")", (gateway,))}
    except sqlite3.Error as exc:
        _log.debug("kanban sync: divergence pre-filter unavailable (%s); scanning every card", exc)
        return None
    finally:
        conn.close()

_TERMINAL = frozenset({"done", "archived"})


def _run_ended(conn: sqlite3.Connection, uid: str) -> bool:
    row = conn.execute("SELECT ended_at FROM task_runs WHERE uid = ?", (uid,)).fetchone()
    return row is not None and row[0] is not None


def _stale_lifecycle(live: sqlite3.Connection, shadow: sqlite3.Connection, row: sqlite3.Row, theirs: sqlite3.Row,
                     run_uid: str | None) -> bool:
    """True when the live status is older than the replica's, not a local edit the service missed.

    ``running`` names an open run: when that run has already ended (in either copy), no local write can have
    produced the live ``running``; the live row missed the end of its own run. This holds even under a live
    ``claim_lock``, since a held claim names an open run. A live non-terminal status over a replica
    ``done``/``archived`` card on the same run is the same gap. Pushing it would reopen the card for every reader,
    because the owner's HLC is newer than the worker's end (hub t_01a1ceaeb6ba593c, 2026-11-12). That second case
    alone is guarded by a live ``claim_lock``: a local worker holding the card right now makes it no stale
    copy. [daveey]"""
    if not run_uid:
        # A wake on a stale copy (current run cleared) over the replica's done card: no reopen follows the
        # completion, so the live status is older, not a local edit (t_01a1d1092f3caa2f). Both logs must hold it:
        # a reopen in the live log (an uncaptured local reopen, which this repair exists to send) wins. [daveey]
        from hermes_cli.kanban_terminal_guard import terminal_hold
        return (theirs["status"] in _TERMINAL and row["status"] not in _TERMINAL and row["status"] != "running"
                and row["claim_lock"] is None and terminal_hold(shadow, row["id"]) is not None
                and terminal_hold(live, row["id"]) is not None)
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


# A run outcome that parks the card -> (its default landing status, the event the park writes).
_PARKS = {"blocked": ("blocked", "blocked"), "review_requested": ("review", "review_requested"),
          "scheduled": ("scheduled", "scheduled"), "continued": ("scheduled", "continued")}
_PARK_EVENTS = {kind: status for status, kind in _PARKS.values()}
# Where a park event can land. The event's own ``status`` wins over the default (``_landing``): ``block_task``
# lands an exhausted budget in ``triage`` under a ``blocked`` event, and ``continue_task`` lands ``ready`` (not a
# park) when it has no wait.
_PARKED = frozenset({"blocked", "review", "scheduled", "triage"})
# A ``blocked`` event that predates ``block_task`` recording its landing: these re-kinds landed ``triage``.
_TRIAGE_REKINDS = frozenset({"continuation_exhausted", "goal_unachievable"})
# Moves that take a card out of a park after its run ended (``kanban_resume_exec._LIFECYCLE_KINDS`` plus the
# wake and sweep events; its ``claimed``/``spawned`` are left out: a later claim opens a new run, so the card's
# current run is no longer the ended one this heal looks at); any later event naming a ``status`` counts too.
_LEFT_PARK = frozenset({
    "unblocked", "promoted", "promoted_manual", "reopened", "continuation_woken", "condition_met",
    "changes_requested", "dependency_wait", "block_loop_detected", "completed", "archived",
    "review_reopened", "triage_swept", "gave_up", "reclaimed", "status",
    "agent_block_escalated", "ask_rechecked", "supervisor_escalated"})  # the triage sweeps
_WARNED_CAP = 4096  # bound on the once-gate's warned (task, run) set
_warned_once: set[tuple[str, int]] = set()
_KEEP = object()  # a column the heal leaves as it is


def _event_order(event: dict) -> tuple[int, int, int]:
    """Origin order of an event: its second, then its uid's UUIDv7 millisecond, then the local id.

    The local id is arrival order: the Board can deliver an ``unblocked`` before the ``blocked`` it follows, and
    both carry the same second. Every synced event's uid is a UUIDv7 minted at its insert on its own gateway, so
    its 48-bit millisecond orders the two. A row with no uid (sync off, or written while the triggers were down)
    falls back to arrival order, and so does a uid that is not a v7 (its leading bits are random, not time) or a
    v7 outside the row's own second: ``kanban_db_sync`` backfills uid-less rows with a v7 stamped at backfill time.
    A uid up to ``UID_STRADDLE_MS`` past the second still counts (created_at is read before the INSERT, the uid at
    it), and sorts last in its own second.
    """
    second = int(event["created_at"] or 0)
    ms = consistent_uid_milliseconds(event.get("uid"), second)
    return second, second * 1000 if ms is None else ms, int(event["id"])


def _payload(event: dict) -> dict:
    """The event's payload, or {} when it is absent or not a JSON object (a malformed row is skipped, not fatal)."""
    try:
        value = json.loads(event["payload"]) if event["payload"] else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _lifecycle(event: dict) -> bool:
    payload = _payload(event)
    if payload.get("metadata_only"):  # ``kanban_human_request_migration.classify`` re-files an ask, no move
        return False
    return event["kind"] in _PARK_EVENTS or event["kind"] in _LEFT_PARK or "status" in payload


def _landing(event: dict) -> str | None:
    """Where a park event left the card: its own recorded ``status``, else its kind's default; None when that is
    not a park (a ``continued`` run with no wait went straight back to ``ready``)."""
    payload = _payload(event)
    status = payload.get("status")
    if not isinstance(status, str):
        status = _PARK_EVENTS[event["kind"]]
        if event["kind"] == "blocked" and payload.get("rekind_reason") in _TRIAGE_REKINDS \
                and not payload.get("shared_blocker_probe"):
            status = "triage"
    return status if status in _PARKED else None


def _parked_status(live: sqlite3.Connection, tid: str, run: sqlite3.Row) -> tuple[str, dict] | None:
    """The park the card sits in after its ended run, with that park's event payload; None when the card left it.

    A ``blocked`` outcome is not proof of ``blocked``: a dependency block lands ``todo``, a loop or an exhausted
    budget ``triage``, so the park's own event (written in the run's end transaction, at or after the run's start)
    must name it. The card's last lifecycle move since then decides: that park event, or a later park event (the
    wake invariant turns a wakeless human ``scheduled`` wait into ``blocked``), lands where it recorded
    (``_landing``); anything else (an unblock, a wake, a review reopen, a triage sweep) means the card left it.
    A ``block_loop_detected`` end is not a park either: the heal reclaims it to ``todo`` like any loop sweep, and
    the loop detector re-parks it if the run loops again. Events are ordered by origin time (``_event_order``)."""
    if (run["outcome"] or "") not in _PARKS:
        return None
    cols = {r[1] for r in live.execute("PRAGMA table_info(task_events)")}
    uid = "uid" if "uid" in cols else "NULL AS uid"
    kinds = sorted(_PARK_EVENTS.keys() | _LEFT_PARK)
    events = [e for e in (dict(r) for r in live.execute(
        f"SELECT id, {uid}, kind, payload, created_at, run_id FROM task_events WHERE task_id = ? "
        f"AND (kind IN ({','.join('?' * len(kinds))}) OR payload LIKE '%\"status\"%')", (tid, *kinds)))
        if _lifecycle(e)]
    end = max((e for e in events if e["kind"] == _PARKS[run["outcome"]][1]
               and (e["run_id"] == run["run_id"] or int(e["created_at"] or 0) >= int(run["started_at"] or 0))),
              key=_event_order, default=None)
    if end is None:
        return None
    last = max(events, key=_event_order)  # ``end`` itself when nothing moved the card since
    status = _landing(last) if last["kind"] in _PARK_EVENTS else None
    return (status, _payload(last)) if status else None


def _park_fields(status: str, payload: dict) -> dict:
    """Columns a restored park takes from its own event, over whatever the stale copy carried.

    ``blocked`` (and an exhausted budget's ``triage``): the block's ``kind`` and ``human_request`` (``block_task``
    writes both in the same transaction as the event; a copy sent before the block carries the pre-park NULLs, and a
    blocked card with no ask is escalated to triage by the agent-block sweep). ``scheduled``: ``schedule_task`` and
    ``continue_task`` clear ``human_request``. The wake itself (``task_wake_checks`` rows, the event's ``wake_at``)
    is not a ``tasks`` column, so no stale task copy changes it; the ask's own probe row is re-synced by
    ``_heal_one``. ``review`` carries no park field."""
    if status == "scheduled":
        return {"human_request": None}
    if status not in ("blocked", "triage"):
        return {}
    fields = {}
    if isinstance(payload.get("kind"), str):
        fields["block_kind"] = payload["kind"]
    if "human_request" in payload:
        ask = payload["human_request"]
        fields["human_request"] = json.dumps(ask) if ask else None
    return fields


def heal_running_over_ended_run(live: sqlite3.Connection) -> dict[str, str]:
    """Move this file's cards that read ``running`` with no claim, no lease and an ended current run to where that
    run left them; returns {task id: new status}.

    No writer produces that shape on purpose: a claim opens its run, a local worker holds ``claim_lock``, a pool
    worker holds the Board lease, and every end of a run moves the card in the same transaction. It is left by a
    stale copy sent over the run's end (hub t_01a1ceaeb6ba593c, 2026-11-12), and the dispatcher's stale-claim sweep
    never sees it (no claim to expire), so the card read ``running`` for good. A run ended ``completed`` with the
    card's latest ``completed`` event naming it is a completion: ``done``. A run that parked the card (``blocked``,
    ``review_requested``, ``scheduled``, ``continued``) puts it back in that park when the park's own event is the
    card's last lifecycle move (``_parked_status``), so a heal never re-dispatches a card that was waiting. Any other
    end is a reclaim: ``todo``, which the promoter turns into ``ready`` once the card's parents are done. The write
    goes through the capture triggers, so the Board's copy is corrected by the same round's push. Each card heals in
    its own transaction, and a card whose heal raises is logged and skipped, never the round. [daveey]"""
    rows = live.execute(
        "SELECT t.id, r.id AS run_id, r.outcome, r.started_at, r.ended_at FROM tasks t "
        "JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.claim_lock IS NULL AND t.lease_holder IS NULL "
        "AND r.ended_at IS NOT NULL").fetchall()
    healed: dict[str, str] = {}
    for row in rows:
        try:
            status = _heal_one(live, row)
        except (sqlite3.Error, ValueError, TypeError) as exc:  # one bad card never stops the others' heal
            _log.warning("kanban sync: heal of %s over its ended run %s failed, skipped: %s",
                         row["id"], row["run_id"], exc)
            continue
        if status is not None:
            healed[row["id"]] = status
            _log.warning("kanban sync: %s read running over its ended run %s (%s); moved to %s",
                         row["id"], row["run_id"], row["outcome"], status)
    return healed


def _heal_one(live: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Heal one card in its own transaction; returns its new status, or None when it was left as it is."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn
    tid = row["id"]
    with write_txn(live):
        # ``run_id`` is the event column (synced as ``run_uid``); the payload key is the pre-column record. A
        # malformed payload reads as no key (``json_extract`` raises on one; CASE evaluates it only when valid).
        if live.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'status_healed' AND (run_id = ? "
                        "OR CASE WHEN json_valid(payload) THEN json_extract(payload, '$.run_id') END = ?)",
                        (tid, row["run_id"], row["run_id"])).fetchone():
            key = (tid, int(row["run_id"]))
            if key not in _warned_once:  # once per (task, run) per process: it stays outvoted every round
                if len(_warned_once) >= _WARNED_CAP:
                    _warned_once.clear()
                _warned_once.add(key)
                _log.warning("kanban sync: %s still reads running over its ended run %s after one heal; the Board "
                             "kept a newer write, not healing it again", tid, row["run_id"])
            return None
        event = live.execute("SELECT run_id FROM task_events WHERE task_id = ? AND kind = 'completed' "
                             "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        done = row["outcome"] == "completed" and event is not None and event["run_id"] == row["run_id"]
        park = None if done else _parked_status(live, tid, row)
        status = "done" if done else park[0] if park else "todo"
        extra = _park_fields(*park) if park else {}
        # A park's own columns (``_park_fields``); a completion clears ``block_kind``; a reclaim keeps the rest.
        extra.setdefault("block_kind", None if done else _KEEP)
        sets = "".join(f", {col} = ?" for col, v in extra.items() if v is not _KEEP)
        values = [v for v in extra.values() if v is not _KEEP]
        changed = live.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            f"completed_at = CASE WHEN ? THEN COALESCE(completed_at, ?) ELSE NULL END{sets} "
            "WHERE id = ? AND status = 'running' AND claim_lock IS NULL AND lease_holder IS NULL "
            "AND current_run_id = ?",
            (status, done, row["ended_at"], *values, tid, row["run_id"])).rowcount
        if not changed:
            return None
        if "human_request" in extra:  # the restored ask's resolved_check probe row, which nobody ran meanwhile
            from hermes_cli.kanban_ask_resolved import sync_row
            sync_row(live, tid, due_now=True)
        kb._append_event(live, tid, "status_healed", {"from": "running", "to": status,
                                                      "outcome": row["outcome"]}, run_id=row["run_id"])
    return status


# rows.ts ``upsertTask``: the holder moved its card out of ``running`` before its own end of the run arrived, so the
# Board ended the lease's run with this placeholder. Its comment: "the holder's own end of the run normally follows".
_LEASE_END_PLACEHOLDER = "card left running (done)"


def heal_clobbered_completions(live: sqlite3.Connection) -> list[str]:
    """Restore completed runs the Board's placeholder lease end overwrote; returns the task ids healed.

    Guarded to the one shape that write leaves: the card is ``done``; the run is its latest; the run reads exactly
    ``released``/``released`` with the Board's placeholder error; and the card's latest ``completed`` event names
    that run (``complete_task`` writes the event and the ``done/completed`` run end in one transaction, so the
    event proves how this holder ended the run). The run is set back to ``done``/``completed`` with the event's
    summary, through the capture triggers, so the Board's copy is corrected by the next push too. Anything else is
    left alone: a run released for any other reason is not a completion. [daveey]"""
    from hermes_cli.kanban_db_connect import write_txn
    rows = live.execute(
        "SELECT r.id AS run_id, r.task_id, r.ended_at, t.completed_at FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE r.status = 'released' AND r.outcome = 'released' AND r.error = ? AND t.status = 'done' "
        "AND r.id = (SELECT MAX(id) FROM task_runs WHERE task_id = r.task_id)", (_LEASE_END_PLACEHOLDER,)).fetchall()
    healed = []
    for row in rows:
        event = live.execute(
            "SELECT run_id, payload FROM task_events WHERE task_id = ? AND kind = 'completed' ORDER BY id DESC LIMIT 1",
            (row["task_id"],)).fetchone()
        if event is None or event["run_id"] != row["run_id"]:
            continue
        try:
            payload = json.loads(event["payload"] or "{}")
        except ValueError:
            payload = {}
        summary = (payload.get("summary") or payload.get("result")) if isinstance(payload, dict) else None
        ended = max(int(row["ended_at"] or 0), int(row["completed_at"] or 0)) or None
        with write_txn(live):
            changed = live.execute(
                "UPDATE task_runs SET status = 'done', outcome = 'completed', error = NULL, "
                "summary = COALESCE(summary, ?), ended_at = ? "
                "WHERE id = ? AND status = 'released' AND outcome = 'released' AND error = ?",
                (summary, ended, row["run_id"], _LEASE_END_PLACEHOLDER)).rowcount
        if changed:
            healed.append(row["task_id"])
            _log.warning("kanban sync: %s run %s was ended by the Board's lease placeholder over its completion; "
                         "restored done/completed", row["task_id"], row["run_id"])
    return healed
