"""Comment-id queries for the live comment bridge (moved out of ``kanban_db``; re-exported there)."""
from __future__ import annotations

import json
import sqlite3
from typing import Optional


def own_comment_ids(conn: sqlite3.Connection, task_id: str, run_id: int) -> set[int]:
    """Ids of the comments on ``task_id`` written from inside run ``run_id`` (any process).

    [daveey] A comment stamped with ``writer_run_uid`` is matched by that stable uid only
    (the payload integer is local to the database that wrote it); legacy uid-less comments
    keep the integer match."""
    try:
        row = conn.execute("SELECT uid FROM task_runs WHERE id = ?", (int(run_id),)).fetchone()
    except sqlite3.OperationalError:
        row = None  # unsynced schema: no uid column
    uid = row[0] if row and isinstance(row[0], str) and row[0] else None
    rows = conn.execute(
        "SELECT json_extract(payload, '$.comment_id') FROM task_events "
        "WHERE task_id = ? AND kind = 'commented' AND ("
        "(json_type(payload, '$.writer_run_uid') IS NULL "
        " AND json_extract(payload, '$.writer_run_id') = ?) "
        "OR (? IS NOT NULL AND json_type(payload, '$.writer_run_uid') = 'text' "
        "    AND json_extract(payload, '$.writer_run_uid') = ?))",
        (task_id, int(run_id), uid, uid),
    ).fetchall()
    return {int(r[0]) for r in rows if r[0] is not None}


def delivered_comment_ids(conn: sqlite3.Connection, task_id: str, run_id: int) -> set[int]:
    """Ids the live bridge already steered into run ``run_id`` (``comment_delivered`` events)."""
    out: set[int] = set()
    for (raw,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'comment_delivered' "
            "AND run_id = ?", (task_id, int(run_id))).fetchall():
        try:
            out.update(int(i) for i in (json.loads(raw or "{}").get("comment_ids") or []))
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def comment_id_before_run_start(conn: sqlite3.Connection, task_id: str, run_id: int) -> Optional[int]:
    """Highest comment id on ``task_id`` created before run ``run_id`` started (0 when none);
    None when the run or its ``started_at`` can't be resolved. Those earlier comments are in
    the worker's prompt, so the live comment bridge seeds its watermark here."""
    row = conn.execute("SELECT started_at FROM task_runs WHERE id = ? AND task_id = ?",
                       (int(run_id), task_id)).fetchone()
    if not row or row[0] is None:
        return None
    got = conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id = ? AND created_at < ?",
                       (task_id, int(row[0]))).fetchone()
    return int(got[0]) if got and got[0] is not None else 0
