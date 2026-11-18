"""Unread-thread policy for manual release surfaces, never autonomous DB callers. [daveey]"""
from __future__ import annotations

import json
import os
import shlex
import time
import uuid
from datetime import UTC, datetime

UNREAD_EXIT = 4


class UnreadComments(Exception):
    """A read-only refusal; raised inside the release transaction to roll it back."""

    def __init__(self, comments, rerun):
        self.comments = comments
        self.rerun = rerun
        lines = ["unread_comments: read the newer comments before releasing this card."]
        for row in comments:
            lines.append(f"Comment {row['id']} | {row['writer_session']} | {row['time']}\n{row['body']}")
        lines.append(f"Rerun after reading: {rerun}")
        super().__init__("\n".join(lines))


def check(conn, tid, *, session, seen=None, verb="unblock", board=None):
    """Called under the status writer's lock; exceptions leave events/comments unchanged."""
    events = conn.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind='commented' "
        "AND coalesce(json_extract(payload, '$.writer_session'), '') != '' "
        "AND json_extract(payload, '$.writer_session') != ? "
        "AND (? IS NOT NULL OR created_at >= ?)",
        (tid, session or "", seen, int(time.time()) - 3600),
    ).fetchall()
    if not events:
        return
    rows = conn.execute("SELECT * FROM task_comments WHERE task_id=? ORDER BY id", (tid,)).fetchall()
    all_events = conn.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind='commented'", (tid,),
    ).fetchall()
    from hermes_cli import kanban_comment_transcript_proof as proof
    local_runs = _local_writer_runs(conn, tid)
    transcripts = {}
    resolved = {}
    for event in all_events:
        payload = json.loads(event["payload"] or "{}")
        writer = (payload.get("writer_run_id"), payload.get("writer_run_uid"))
        local_writer = all(isinstance(v, (int, str)) for v in writer) and writer in local_runs
        resolved[event["id"]] = _resolve(rows, event, payload, local_writer=local_writer)
        session_id = payload.get("writer_session")
        if (not resolved[event["id"]] and not payload.get("comment_uid")
                and isinstance(session_id, str) and session_id):
            if session_id not in transcripts:
                transcripts[session_id] = proof.messages(session_id)
            resolved[event["id"]] = proof.resolve(rows, event, payload, transcripts[session_id], board=board)
    missing = _missing_coverage(resolved)
    comments = []
    for event in events:
        payload = json.loads(event["payload"])
        candidates = resolved[event["id"]]
        if event["id"] in missing:
            # A partial sync must not turn missing comment data into permission.
            comments.append(_listing(None, "Comment data unavailable; wait for sync and read again. --seen cannot "
                                     "acknowledge missing data.", event, payload))
        for row in candidates:
            if seen is None or row["id"] > seen:
                comments.append(_listing(row["id"], row["body"], event, payload))
    if not comments:
        return
    comments = list({(r["id"], r["writer_session"]): r for r in comments}.values())
    command = ["hermes", "kanban"]
    if board:
        command += ["--board", board]
    newest = max((r["id"] for r in comments if r["id"] is not None), default=seen or 0)
    command += [verb, tid, "--seen", str(newest)]
    raise UnreadComments(comments, shlex.join(command))


def _missing_coverage(resolved):
    """Reject ambiguous components without distinct rows for every event.

    Own/old events still consume rows: filtering them before matching lets their
    rows stand in for a missing foreign comment. Matching proves completeness,
    not identity, so the caller still lists *all* plausible legacy candidates.
    """
    by_row = {}
    for eid, rows in resolved.items():
        for row in rows:
            by_row.setdefault(row["id"], set()).add(eid)
    remaining, missing = set(resolved), set()
    while remaining:
        component, pending = set(), {next(iter(remaining))}
        while pending:
            eid = pending.pop()
            component.add(eid)
            for row in resolved[eid]:
                pending.update(by_row[row["id"]] - component)
        remaining -= component
        owners = {}
        if not all(_match(eid, resolved, owners) for eid in component):
            missing.update(component)
    return missing


def _match(start, resolved, owners):
    """Iterative augmenting path; no recursion limit for large comment threads."""
    pending, visited_rows = [start], set()
    previous: dict[int, tuple[int, int] | None] = {start: None}
    for eid in pending:
        for row in resolved[eid]:
            rid = row["id"]
            if rid in visited_rows:
                continue
            visited_rows.add(rid)
            owner = owners.get(rid)
            if owner is None:
                while True:
                    owners[rid] = eid
                    step = previous[eid]
                    if step is None:
                        return True
                    eid, rid = step
            if owner not in previous:
                previous[owner] = (eid, rid)
                pending.append(owner)
    return False


def _listing(cid, body, event, payload):
    return {"id": cid, "writer_session": payload["writer_session"],
            "time": datetime.fromtimestamp(event["created_at"], UTC).isoformat(), "body": body[:300]}


def _local_writer_runs(conn, tid):
    """Prove the writer's integer namespace using its portable local claim receipt.

    NULL event origin and a ``local-`` session prefix are not host identity:
    imports can have both. A foreign run must never lend its comment_id to a
    colliding local row, even when its run integer happens to collide too.
    """
    from hermes_cli.kanban_db_sync import sync_state
    state = sync_state(conn)
    if not state or not state.get("gateway_id"):
        return set()
    return {(row[0], row[1]) for row in conn.execute(
        "SELECT r.id, r.uid FROM task_runs r WHERE r.task_id=? AND r.uid IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=r.task_id AND e.kind='claimed' "
        "AND json_extract(e.payload, '$.run_uid')=r.uid "
        "AND json_extract(e.payload, '$.holder')=?)", (tid, state["gateway_id"]))}


def _resolve(rows, event, payload, *, local_writer=False):
    """Never compare a wire-local integer with a receiver's watermark.

    Legacy synced events have no portable reference. Require acknowledgement of
    every plausible same-time/author row rather than guess among collisions.
    The timestamp tolerance covers the writer crossing a second between inserts;
    UUIDv7 write times cover legacy rows stamped before the write lock (``_same_write``).
    Body length cannot disambiguate because add_comment strips stored text.
    """
    if payload.get("comment_uid"):
        return [r for r in rows if "uid" in r.keys() and r["uid"] == payload["comment_uid"]]
    if rows and "uid" not in rows[0].keys():  # never-synced board: IDs are local
        return [r for r in rows if r["id"] == payload.get("comment_id")]
    if local_writer and isinstance(payload.get("author"), str) and payload["author"].strip():
        # add_comment writes the row before the event; scheduling/IO may delay
        # the second timestamp. Local run provenance makes its integer usable.
        return [r for r in rows if r["id"] == payload.get("comment_id")
                and r["author"] == payload["author"].strip()]
    return [r for r in rows if (abs(r["created_at"] - event["created_at"]) <= 1 or _same_write(r, event))
            and (not payload.get("author") or r["author"] == payload["author"].strip())]


def _uuid7_ms(uid):
    """Millisecond write time a UUIDv7 carries; None for any other value."""
    try:
        value = uuid.UUID(str(uid))
    except (ValueError, TypeError, AttributeError):
        return None
    return value.int >> 80 if value.version == 7 else None


def _same_write(row, event):
    """Row and event written in one ``add_comment`` transaction, proven by write clocks.

    Legacy ``add_comment`` stamped the row's ``created_at`` before waiting for the
    write lock and the event's after it, so the integers drift by seconds (5 s and
    7 s seen live) while the UUIDv7 uids, minted inside the transaction, sit
    milliseconds apart. A uid only records the write when it was minted then: the
    sync-enable backfill gives every old row and event a uid of the enable instant,
    so the event's uid time must agree with its own ``created_at`` (a floor, so
    within [0, 2 s)), the row must be stamped at most ``_LOCK_DRIFT`` before the
    event, and the two uids within a second.
    """
    keys = row.keys()
    row_ms = _uuid7_ms(row["uid"]) if "uid" in keys else None
    event_ms = _uuid7_ms(event["uid"]) if "uid" in event.keys() else None
    if row_ms is None or event_ms is None:
        return False
    minted_at_write = 0 <= event_ms - event["created_at"] * 1000 < 2000
    drift = event["created_at"] - row["created_at"]
    return minted_at_write and 0 <= drift <= _LOCK_DRIFT and abs(row_ms - event_ms) <= 1000


_LOCK_DRIFT = 60  # seconds a legacy row stamp may precede its event (write-lock wait)


def cli_guard(args, tid, verb):
    """Build a transactional precondition using the CLI's inherited caller identity."""
    return lambda conn: check(conn, tid, session=os.environ.get("HERMES_SESSION_ID"),
                              seen=getattr(args, "seen_comment_id", None), verb=verb,
                              board=getattr(args, "board", None))


def capture_refusal(op, tid, refused):
    """Keep bulk operations per-card: a refusal must not hide earlier successes."""
    try:
        return op()
    except UnreadComments as exc:
        refused[tid] = {"rejection": "unread_comments", "error": str(exc),
                        "comments": exc.comments, "rerun": exc.rerun}
        return False
