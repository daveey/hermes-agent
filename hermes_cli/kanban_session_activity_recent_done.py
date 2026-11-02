"""Done cards a session created or shepherds, completed since a cutoff (the attention pass's board read).

Split from ``kanban_session_activity`` (its file cap); ``_read_recent_done`` there delegates here. [daveey]
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

from hermes_cli import kanban_lockguard as _kanban_lockguard


def read_recent_done(path: Path, since: float, counted_sub_clause: Callable[[sqlite3.Connection], str],
                     board_slug: Callable[[Path], str]) -> list[tuple[str, dict[str, Any]]]:
    """``(session_id, card)`` for done cards completed since ``since`` that a session created or shepherds."""
    conn = _kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
    conn.row_factory = sqlite3.Row
    try:
        has_runs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'").fetchone() is not None
        summary = ("COALESCE((SELECT r.summary FROM task_runs r WHERE r.task_id = t.id AND r.summary IS NOT NULL "
                   "AND r.summary != '' ORDER BY r.id DESC LIMIT 1), t.result)") if has_runs else "t.result"
        fields = f"t.id AS id, t.title AS title, t.completed_at AS completed_at, {summary} AS summary"
        cond = "t.status = 'done' AND t.completed_at >= ?"
        pairs = [(str(r["session_id"]), dict(r)) for r in conn.execute(
            f"SELECT t.session_id AS session_id, {fields} FROM tasks t WHERE t.session_id IS NOT NULL "
            f"AND t.session_id != '' AND {cond}", (since,))]
        try:
            pairs.extend((str(r["chat_id"]), dict(r)) for r in conn.execute(
                f"SELECT s.chat_id AS chat_id, {fields} FROM kanban_notify_subs s JOIN tasks t ON t.id = s.task_id "
                f"WHERE s.platform = 'tui' AND s.chat_id != '' AND {counted_sub_clause(conn)} AND {cond}",
                (since,)))
        except sqlite3.Error:  # board predates notify subscriptions
            pass
    finally:
        conn.close()
    board = board_slug(path)
    out = []
    for sid, card in pairs:
        card.pop("session_id", None)
        card.pop("chat_id", None)
        out.append((sid, {**card, "board": board, "board_path": str(path)}))
    return out
