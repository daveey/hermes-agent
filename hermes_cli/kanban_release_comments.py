"""Unread-thread policy for manual release surfaces, never autonomous DB callers. [daveey]"""
from __future__ import annotations

import json
import os
import shlex
import time
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
    resolved = {e["id"]: _resolve(rows, e, json.loads(e["payload"] or "{}")) for e in all_events}
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


def _resolve(rows, event, payload):
    """Never compare a wire-local integer with a receiver's watermark.

    Legacy synced events have no portable reference. Require acknowledgement of
    every plausible same-time/author row rather than guess among collisions.
    The timestamp tolerance covers the writer crossing a second between inserts;
    body length cannot disambiguate because add_comment strips stored text.
    """
    if payload.get("comment_uid"):
        return [r for r in rows if "uid" in r.keys() and r["uid"] == payload["comment_uid"]]
    if rows and "uid" not in rows[0].keys():  # never-synced board: IDs are local
        return [r for r in rows if r["id"] == payload.get("comment_id")]
    return [r for r in rows if abs(r["created_at"] - event["created_at"]) <= 1
            and (not payload.get("author") or r["author"] == payload["author"].strip())]


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
