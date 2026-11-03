"""Respawn-guard classifiers for events that make a re-spawn deliberate rather than a duplicate. [daveey]

Moved out of ``kanban_db_dispatch`` (file cap): :func:`reopened_after` (operator reopen of a done
card, used by ``recent_success`` and ``active_pr``) and :func:`is_handoff_event` (``active_pr``).
"""
from __future__ import annotations

import sqlite3
from typing import Optional


def reopened_after(conn: sqlite3.Connection, task_id: str, since: int) -> bool:
    """A supported operator reopen (:func:`kanban_db_operator.reopen_done_task`: ``done`` -> ready/todo,
    payload ``previous_status: done``) strictly after ``since``: a deliberate "run it again". A same-second
    tie, an older reopen, or a ``reopened`` event without that payload fails closed. One reopen authorizes
    a rerun until that rerun completes or posts a PR comment (newer, so it guards again); a rerun that
    ends without either retries like any reclaimed run. [daveey]"""
    from hermes_cli import kanban_db as _kb
    from hermes_cli.kanban_db_dispatch import _rows_since

    return any(_kb._json_dict(e["payload"]).get("previous_status") == "done" for _, e in _rows_since(
        conn, "task_events", task_id, since, "AND kind = 'reopened'", cols=", payload", strict=True))


def is_handoff_event(kind: str, payload: Optional[str]) -> bool:
    """Only an ``assigned`` event that moves the card to a DIFFERENT profile is
    a handoff. A no-op re-assign (dev→dev via CLI/dashboard/``reassign
    --reclaim``), an unassign, or the dispatcher's own
    ``kanban.default_assignee`` write would otherwise lift ``active_pr`` for
    the very implementer that opened the PR. Events without ``from`` (written
    before it was recorded) are not trusted as handoffs — fail closed."""
    if kind != "assigned":
        return True
    from hermes_cli import kanban_db as _kb

    data = _kb._json_or(payload, {})
    if not isinstance(data, dict) or data.get("source") == "kanban.default_assignee":
        return False
    to = data.get("assignee")
    return bool(to) and "from" in data and data["from"] != to
