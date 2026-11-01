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

def _current_guard_hold(
    conn: sqlite3.Connection, task_id: str, *, include_replies: bool = False,
) -> list[tuple[int, int, str, str]]:
    """Current hold in occurrence order; sync imports can give old events newer local IDs."""
    from hermes_cli import kanban_db as _kb

    comment_filter = (
        "NOT (kind = 'commented' AND json_extract(payload, '$.author') = 'kanban-dispatcher')"
        if include_replies else "kind != 'commented'"
    )
    events = sorted(
        (stamp, row[0], row[1], row[2])
        for row in conn.execute(
            "SELECT id, kind, payload, created_at FROM task_events "
            f"WHERE task_id = ? AND {comment_filter}", (task_id,),
        )
        if (stamp := _kb._to_epoch(row[3])) is not None
    )
    hold = []
    # IDs only break ties within the same normalized second, never order older imports.
    for event in events:
        if event[2] == "respawn_guarded":
            hold.append(event)
        else:
            hold.clear()
    return hold


def guard_hold_started_at(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """When the current unbroken run of ``respawn_guarded`` events began: the first one
    after the newest event of any other kind (a claim, a block, an unblock...). Comments of any
    author do not restart it: a note or reply fixes nothing, and resetting on them held
    t_d5d83916 in ``ready`` 374 min after its first guard event (09:24Z). [daveey]"""
    hold = _current_guard_hold(conn, task_id)
    return hold[0][0] if hold else None


def guard_hold_reason(conn: sqlite3.Connection, task_id: str, *, include_replies: bool = True) -> Optional[str]:
    """The reason of the latest guard, ignoring dispatcher notice comments.
    Other authors' replies still cause a fresh guard event on the next held tick, but do not
    reset the hold clock. One unbroken hold otherwise logs one event per reason, not per tick.
    """
    hold = _current_guard_hold(conn, task_id, include_replies=include_replies)
    if not hold:
        return None
    try:
        data = json.loads(hold[-1][3] or "{}")
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
    from hermes_cli.kanban_wake_check import foreign_copy

    # Pickup imports the hold clock, not maintenance authority. Local comment
    # and create idempotency checks cannot deduplicate across unsynced gateways.
    if foreign_copy(conn, task_id):
        return False
    if reason in _kbd._GUARD_HOLD_EXEMPT_REASONS:
        return False
    if reason == "rate_limit_cooldown":
        # A quota episode's backoff (up to 2 h) is self-clearing and posts its own notice after
        # three requeues; a generic fix card would ask an agent to fix a provider's quota.
        from hermes_cli.kanban_quota_episode import active
        if active(conn, task_id):
            return False
    started = guard_hold_started_at(conn, task_id)
    if started is None:
        return False
    held = int(time.time()) - started
    if held < _kbd._env_seconds("HERMES_KANBAN_GUARD_HOLD_NOTIFY_SECONDS", DEFAULT_GUARD_HOLD_NOTIFY_SECONDS):
        return False
    if any(
        stamp >= started
        for row in conn.execute(
            "SELECT created_at FROM task_comments WHERE task_id = ? AND body LIKE ?",
            (task_id, f"{GUARD_HOLD_NOTE_MARKER}%"),
        )
        if (stamp := _kb._to_epoch(row[0])) is not None
    ):
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


def sweep_remote_guard_holds(conn: sqlite3.Connection, result) -> None:
    """Home maintenance for cards filtered out of this machine's execution lanes.

    The executor's synced, unbroken hold is the evidence. Revalidate against
    synced runs/errors so an expired cooldown is not mistaken for a live hold.
    """
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli.kanban_wake_check import foreign_ids

    foreign = foreign_ids(conn)
    for lane in ("ready", "review"):
        for task_id in dispatch._local_refusals(conn, lane):
            if task_id in foreign:
                continue
            try:
                reason = guard_hold_reason(conn, task_id, include_replies=False)
                if reason is None or dispatch.check_respawn_guard(conn, task_id, lane=lane) != reason:
                    continue
                if dispatch._block_if_guard_hold_capped(conn, task_id, reason):
                    result.auto_blocked.append(task_id)
                else:
                    notify_guard_hold_safely(conn, task_id, reason)
            except sqlite3.Error:
                logger.exception("kanban: remote guard maintenance for %s failed", task_id)


def record_guard_hold(conn: sqlite3.Connection, task_id: str, reason: str) -> None:
    """Per held tick: the hold notice (past the threshold), then a ``respawn_guarded`` event only
    when the hold is new or its reason changed — one event per unbroken hold, not one per tick
    (~1400 per card before). [daveey]"""
    from hermes_cli import kanban_db as _kb

    notify_guard_hold_safely(conn, task_id, reason)
    if guard_hold_reason(conn, task_id) != reason:
        with _kb.write_txn(conn):
            _kb._append_event(conn, task_id, "respawn_guarded", {"reason": reason})
