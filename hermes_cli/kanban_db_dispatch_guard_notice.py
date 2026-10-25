"""Respawn-guard hold notice: one kanban comment + one fix card per unbroken hold. [daveey]

Well before the hold cap, a held card must be visible: past
``HERMES_KANBAN_GUARD_HOLD_NOTIFY_SECONDS`` (default 30 min) of unbroken hold the dispatcher
posts ONE kanban comment + a fix card (assignee default) naming the guard reason and, for
``active_pr``, the PR and its failing checks (2026-10-21: t_806b9e8f sat held 360 min on a PR
with 11 failing checks and nothing said so). Never an Inbox card: the Inbox is reserved for
owner-only decisions (AGENTS.md) and a guard hold is agent-fixable board state. Split out of
``kanban_db_dispatch`` (code-health file cap).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_GUARD_HOLD_NOTIFY_SECONDS = 30 * 60
GUARD_HOLD_NOTE_MARKER = "[respawn-guard]"

def guard_hold_started_at(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """When the current unbroken run of ``respawn_guarded`` events began: the first one
    after the newest event of any other kind (a claim, a block, an unblock, a comment...)."""
    row = conn.execute(
        "SELECT MIN(created_at) FROM task_events WHERE task_id = ? AND kind = 'respawn_guarded' "
        "AND id > COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = ? "
        "AND kind != 'respawn_guarded' AND NOT (kind = 'commented' AND "
        "json_extract(payload, '$.author') = 'kanban-dispatcher')), 0)", (task_id, task_id),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def guard_hold_reason(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The reason of the hold in progress, or None: the newest event (ignoring the dispatcher's own
    notice comments, as :func:`guard_hold_started_at` does) when it is a ``respawn_guarded`` one.
    The dispatcher appends a new ``respawn_guarded`` event only when this differs, so an unbroken
    hold logs one event per reason instead of one per tick."""
    row = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND NOT (kind = 'commented' AND "
        "json_extract(payload, '$.author') = 'kanban-dispatcher') ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row is None or row[0] != "respawn_guarded":
        return None
    try:
        data = json.loads(row[1] or "{}")
    except ValueError:
        return None
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if isinstance(reason, str) else None


def _pr_failing_checks(pr_url: str) -> str:
    """Best-effort one-line summary of ``pr_url``'s failing checks ('' if unavailable)."""
    try:
        out = subprocess.run(["gh", "pr", "checks", pr_url], stdin=subprocess.DEVNULL, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    failing = [ln.split("\t")[0].strip() for ln in out.splitlines()
               if len(ln.split("\t")) > 1 and ln.split("\t")[1].strip().lower() in ("fail", "failure", "cancel")]
    return f"{len(failing)} failing check(s): " + ", ".join(failing[:12]) if failing else ""


def _file_fix_card(conn: sqlite3.Connection, task_id: str, reason: str, body: str) -> None:
    """Agent-fixable hold -> a Kanban card for the default assignee (never the Inbox: David
    muted the ``kanban-dispatcher`` poster after 3 unactioned cards). Idempotent per hold."""
    from hermes_cli import kanban_db as _kb

    try:
        _kb.create_task(
            conn, title=f"Fix respawn-guard hold ({reason}) on {task_id}"[:120],
            body=(f"{body}\n\nRead kanban card {task_id}'s latest [respawn-guard] comment, fix the cause "
                  f"(failing PR checks, binding) and unblock it. Only ask David for a decision he alone can make."),
            assignee="default", created_by="kanban-dispatcher", idempotency_key=f"guard-hold:{task_id}")
    except Exception:
        logger.warning("kanban: guard-hold fix card for %s failed", task_id, exc_info=True)


def notify_guard_hold(conn: sqlite3.Connection, task_id: str, reason: str) -> bool:
    """Once per unbroken hold, past the notify threshold: comment + fix card. [daveey]"""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_dispatch as _kbd

    if reason in _kbd._GUARD_HOLD_EXEMPT_REASONS:
        return False
    started = guard_hold_started_at(conn, task_id)
    if started is None:
        return False
    held = int(time.time()) - started
    if held < _kbd._env_seconds("HERMES_KANBAN_GUARD_HOLD_NOTIFY_SECONDS", DEFAULT_GUARD_HOLD_NOTIFY_SECONDS):
        return False
    if conn.execute("SELECT 1 FROM task_comments WHERE task_id = ? AND body LIKE ? AND created_at >= ? LIMIT 1",
                    (task_id, f"{GUARD_HOLD_NOTE_MARKER}%", started)).fetchone():
        return False
    detail = ""
    if reason == "active_pr":
        urls = [m.group(0) for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC", (task_id,))
            for m in [_kbd._RESPAWN_GUARD_PR_URL_RE.search(_kb._lossy_text(r["body"]) or "")] if m]
        if urls:
            checks = _pr_failing_checks(urls[0]) or "failing checks unavailable"
            detail = f" PR: {urls[0]} ({checks})."
    minutes = max(1, held // 60)
    body = (f"{GUARD_HOLD_NOTE_MARKER} Held in ready by the respawn guard ({reason}) for {minutes} min "
            f"without running.{detail} It will be auto-blocked at the hold cap; fix the cause "
            f"(merge/close the PR, rebind, or unblock) before then.")
    _kb.add_comment(conn, task_id, "kanban-dispatcher", body)
    _file_fix_card(conn, task_id, reason, body)
    return True


def notify_guard_hold_safely(conn: sqlite3.Connection, task_id: str, reason: str) -> bool:
    """``notify_guard_hold`` for the dispatch tick: a failed notice never stops the tick."""
    try:
        return notify_guard_hold(conn, task_id, reason)
    except Exception:
        logger.warning("kanban: guard-hold notice for %s failed", task_id, exc_info=True)
        return False


def record_guard_hold(conn: sqlite3.Connection, task_id: str, reason: str) -> None:
    """Per held tick: the hold notice (past the threshold), then a ``respawn_guarded`` event only
    when the hold is new or its reason changed — one event per unbroken hold, not one per tick
    (~1400 per card before). [daveey]"""
    from hermes_cli import kanban_db as _kb

    notify_guard_hold_safely(conn, task_id, reason)
    if guard_hold_reason(conn, task_id) != reason:
        with _kb.write_txn(conn):
            _kb._append_event(conn, task_id, "respawn_guarded", {"reason": reason})
