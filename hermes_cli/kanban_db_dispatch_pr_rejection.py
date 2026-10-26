"""Merge-queue rejection evidence for the respawn guard. [daveey]"""
from __future__ import annotations

import json
import re
import sqlite3

_MARKER = re.compile(r"<!--\s*bb-land\s+(.*?)\s*-->", re.DOTALL)


def rejection_reason(comments: list) -> str | None:
    """Inspect the newest marker only (GitHub returns comments oldest first).

    A malformed newest marker fails closed; never fall back to an older rejection.
    Ordinary review comments do not supersede queue state.
    """
    for comment in reversed(comments):
        body = comment.get("body", "") if isinstance(comment, dict) else ""
        if not isinstance(body, str):
            continue
        starts = list(re.finditer(r"<!--\s*bb-land\b", body))
        if not starts:
            continue
        match = _MARKER.match(body, starts[-1].start())
        if match is None:
            return None
        try:
            result = json.loads(match.group(1))
        except ValueError:
            return None
        if not isinstance(result, dict) or result.get("done") is not True:
            return None
        if result.get("state") != "rejected" and result.get("exit") != 2:
            return None
        return _MARKER.sub("", body).strip()[:4000] or "Merge queue rejected this PR (exit 2)."
    return None


def comment_rejection(conn: sqlite3.Connection, task_id: str, url: str, reason: str) -> None:
    """Persist one repair handoff per distinct rejection, without per-tick spam."""
    from hermes_cli import kanban_db as kb

    body = (f"Respawn guard released rejected merge-queue PR {url}. "
            "Repair and resubmit this PR; do not open a duplicate.\n\n"
            + "\n".join("> " + line for line in reason.splitlines()))
    if not conn.execute("SELECT 1 FROM task_comments WHERE task_id = ? AND body = ?",
                        (task_id, body)).fetchone():
        kb.add_comment(conn, task_id, "respawn-guard", body)
