"""Per listed card, what the linked-card rows show (split out of ``kanban_session_activity``). [daveey]"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from hermes_cli.kanban_run_machine import add_run_machines

_PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_NOTE_CHARS = 200


def _first_line(text: Any) -> str:
    line = str(text or "").strip().split("\n", 1)[0].strip()
    return line[:_NOTE_CHARS]


def _add_card_details(conn: sqlite3.Connection, found: dict[str, dict[str, Any]]) -> None:
    """Per listed card, what the linked-card rows show: the latest event (``activity_kind``/``activity_at``), a
    one-line ``note`` (the newest run summary or comment) and the newest GitHub PR ``pr_url`` found in its
    comments, result or run summaries. One query per table for all listed cards; missing tables are skipped."""
    if not found:
        return
    ids = list(found)
    ph = ",".join("?" for _ in ids)

    def rows(sql: str) -> list[sqlite3.Row]:
        try:
            return conn.execute(sql, ids).fetchall()
        except sqlite3.Error:
            return []

    for r in rows(f"SELECT task_id, kind, MAX(created_at) AS at FROM task_events WHERE task_id IN ({ph}) GROUP BY task_id"):
        found[r["task_id"]].update(activity_kind=r["kind"], activity_at=r["at"])
    notes: dict[str, tuple[Any, str]] = {}
    prs: dict[str, tuple[Any, str]] = {}

    def offer(task_id: str, at: Any, text: Any) -> None:
        at = at or 0
        line = _first_line(text)
        if line and at >= notes.get(task_id, (-1, ""))[0]:
            notes[task_id] = (at, line)
        for url in _PR_URL_RE.findall(str(text or "")):
            if at >= prs.get(task_id, (-1, ""))[0]:
                prs[task_id] = (at, url)

    for r in rows(f"SELECT task_id, COALESCE(ended_at, started_at) AS at, summary FROM task_runs "
                  f"WHERE task_id IN ({ph}) AND summary IS NOT NULL"):
        offer(r["task_id"], r["at"], r["summary"])
    for r in rows(f"SELECT task_id, created_at AS at, body FROM task_comments WHERE task_id IN ({ph})"):
        offer(r["task_id"], r["at"], r["body"])
    for r in rows(f"SELECT id AS task_id, completed_at AS at, result FROM tasks WHERE id IN ({ph}) AND result IS NOT NULL"):
        for url in _PR_URL_RE.findall(str(r["result"] or "")):
            if (r["at"] or 0) >= prs.get(r["task_id"], (-1, ""))[0]:
                prs[r["task_id"]] = (r["at"] or 0, url)
    for task_id, (_, line) in notes.items():
        found[task_id]["note"] = line
    for task_id, (_, url) in prs.items():
        found[task_id]["pr_url"] = url
    add_run_machines(conn, found)  # where each card runs: the strip's hub/worker mark
