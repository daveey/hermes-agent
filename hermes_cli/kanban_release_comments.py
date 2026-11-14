"""Unread-thread policy for manual release surfaces, never autonomous DB callers. [daveey]"""
from __future__ import annotations

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
    rows = conn.execute(
        "SELECT c.id, c.body, e.created_at, json_extract(e.payload, '$.writer_session') AS writer "
        "FROM task_events e JOIN task_comments c ON c.task_id=e.task_id "
        "AND c.id=json_extract(e.payload, '$.comment_id') "
        "WHERE e.task_id=? AND e.kind='commented' "
        "AND coalesce(json_extract(e.payload, '$.writer_session'), '') != '' "
        "AND json_extract(e.payload, '$.writer_session') != ? "
        "AND ((? IS NOT NULL AND c.id > ?) OR (? IS NULL AND e.created_at >= ?)) "
        "ORDER BY c.id", (tid, session or "", seen, seen, seen, int(time.time()) - 3600),
    ).fetchall()
    if not rows:
        return
    comments = [{"id": r["id"], "writer_session": r["writer"],
                 "time": datetime.fromtimestamp(r["created_at"], UTC).isoformat(),
                 "body": r["body"][:300]} for r in rows]
    command = ["hermes", "kanban"]
    if board:
        command += ["--board", board]
    command += [verb, tid, "--seen", str(max(r["id"] for r in rows))]
    raise UnreadComments(comments, shlex.join(command))


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
