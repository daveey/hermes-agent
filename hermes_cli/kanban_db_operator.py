"""Operator edits of a Kanban card: the retry/goal/runtime/session columns
``hermes kanban edit`` exposes, and reopening a done card (``hermes kanban
reopen``). The supported route for writes skills used to do with raw sqlite3,
which sqlite-live-guard refuses. [daveey]"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

from hermes_cli import kanban_db as _kb


class KanbanEditRefused(ValueError):
    """An operator edit refused by a guard (bad value, protected overwrite)."""


def edit_operator_fields(
    conn: sqlite3.Connection, task_id: str, *, max_retries: Optional[int] = None,
    goal_mode: Optional[bool] = None, goal_max_turns: Optional[int] = None, review_required: Optional[bool] = None,
    max_runtime_seconds: Optional[int] = None, session_id: Optional[str] = None,
    force: bool = False, board: Optional[str] = None,
) -> Optional[dict[str, dict[str, Any]]]:
    """Set the operator-only columns ``hermes kanban edit`` exposes (retry
    breaker, goal loop, runtime cap, session backfill) in one txn. ``None``
    leaves a field alone. Returns ``{field: {"old", "new"}}`` for the fields
    that actually changed (``{}`` = nothing to do), ``None`` for an unknown id.
    Raises :class:`KanbanEditRefused` for a non-positive count or for
    overwriting a non-empty ``session_id`` without ``force``. Appends one
    ``edited`` event listing old -> new. [daveey]"""
    wanted: dict[str, Any] = {}
    for field, value in (("max_retries", max_retries), ("goal_max_turns", goal_max_turns),
                         ("max_runtime_seconds", max_runtime_seconds)):
        if value is None:
            continue
        if isinstance(value, bool) or int(value) < 1:
            raise KanbanEditRefused(f"{field} must be a positive integer (got {value!r})")
        wanted[field] = int(value)
    if goal_mode is not None:
        wanted["goal_mode"] = 1 if goal_mode else 0
    if review_required is not None:
        from hermes_cli.kanban_review_required import OPERATOR, worker_identity
        if not review_required and worker_identity():  # any worker run, orchestrators included
            raise KanbanEditRefused(f"review_required can only be disabled by {OPERATOR}")
        wanted["review_required"] = int(bool(review_required))
    if session_id is not None:
        sid = str(session_id).strip()
        if not sid:
            raise KanbanEditRefused("session_id must be non-empty")
        wanted["session_id"] = sid
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT max_retries, goal_mode, goal_max_turns, max_runtime_seconds, session_id, review_required "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return None
        old_sid = (row["session_id"] or "").strip()
        if "session_id" in wanted and old_sid and old_sid != wanted["session_id"] and not force:
            raise KanbanEditRefused(
                f"{task_id} already has session_id {old_sid}; pass --force to overwrite it"
            )
        changes = {
            field: {"old": row[field], "new": new}
            for field, new in wanted.items() if row[field] != new
        }
        if not changes:
            return {}
        conn.execute(
            f"UPDATE tasks SET {', '.join(f'{f} = ?' for f in changes)} WHERE id = ?",
            (*(c["new"] for c in changes.values()), task_id),
        )
        _kb._append_event(conn, task_id, "edited", {"fields": list(changes), "changes": changes})
    _kb.notify_task_updated(conn, task_id, list(changes), board=board)
    return changes


def set_completion_contract(
    conn: sqlite3.Connection, task_id: str, contract: str, *, reason: str, actor: str,
    board: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Operator re-point of a card's completion contract (``hermes kanban edit
    --completion-contract``). The PR gate binds a published PR once and a worker
    can't replace it, so a contract that can never pass (the repo was suspended or
    moved, or its base branch has no required checks) used to park a finished card
    on a human ask forever. ``contract`` is validated like ``kanban_create``'s
    (``local-only``, ``OWNER/REPO`` or an exact PR URL). Refuses
    (:class:`KanbanEditRefused`) an empty ``reason``, an invalid value, or a
    ``done``/``archived`` card. Returns ``{"old", "new"}`` (``{}`` when unchanged),
    ``None`` for an unknown id. Appends a ``contract_changed`` event (from, to,
    reason, actor) and a ``CONTRACT CHANGED:`` comment. [daveey]"""
    from hermes_cli.kanban_pr_acceptance import validate_contract

    reason = (reason or "").strip()
    actor = (actor or "").strip() or "operator"
    if not reason:
        raise KanbanEditRefused("--completion-contract requires --reason (recorded as an event and a comment)")
    try:
        new = validate_contract((contract or "").strip())
    except ValueError as exc:
        raise KanbanEditRefused(str(exc)) from None
    now = int(time.time())
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT status, completion_contract FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] in ("done", "archived"):
            raise KanbanEditRefused(
                f"{task_id} is {row['status']}; a closed card's completion contract is history"
            )
        old = (row["completion_contract"] or "").strip() or "local-only"
        if old == new:
            return {}
        conn.execute("UPDATE tasks SET completion_contract = ? WHERE id = ?", (new, task_id))
        _kb._append_event(conn, task_id, "contract_changed", {
            "from": old, "to": new, "reason": reason[:2000], "actor": actor,
        })
        _kb._insert_comment(conn, task_id, actor, f"CONTRACT CHANGED: {old} -> {new}: {reason}", now)
    _kb.notify_task_updated(conn, task_id, ["completion_contract"], board=board)
    return {"old": old, "new": new}


def reopen_done_task(
    conn: sqlite3.Connection, task_id: str, *, reason: str, actor: str,
) -> Optional[str]:
    """Operator reopen: ``done`` -> ``ready`` (``todo`` while a parent is open,
    the same re-gate unblock uses), ``completed_at`` cleared, the breaker reset.
    Refuses (returns ``None``) any other status or an unknown id, except a
    blocked/scheduled/triage row the event log holds done (``kanban_terminal_guard.stale_hold``). Appends a
    ``reopened`` event with ``reason`` and ``actor`` plus a ``REOPENED:`` comment,
    and invalidates done/ready/running descendants through
    :func:`invalidate_descendants_for_parent_reopen` (every done-reopen surface
    routes there). Returns the landing status. [daveey]"""
    reason = (reason or "").strip()
    actor = (actor or "").strip() or "operator"
    if not reason:
        raise KanbanEditRefused("reopen requires a reason")
    from hermes_cli.kanban_terminal_guard import stale_hold
    with _kb.write_txn(conn):
        # A blocked/scheduled/triage row held done by its event log (a stale copy; ``unblock_task`` refuses it and
        # points here) is reopened too: otherwise no verb revives it.
        previous = _kb._task_status(conn, task_id)
        if previous != "done" and (stale_hold(conn, task_id) or {}).get("status") != "done":
            return None
        new_status = _kb._landing_status_after_parents(conn, task_id)
        # The UPDATE nulls ``current_run_id``: close the run it points at first, as ``unblock_task`` does.
        _kb._reclaim_dangling_run(conn, task_id, statuses=("blocked", "scheduled", "triage"), now=int(time.time()),
                                  note="invariant recovery on reopen")
        cur = conn.execute(
            "UPDATE tasks SET status = ?, completed_at = NULL, current_run_id = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
            "consecutive_failures = 0, human_request = NULL "
            "WHERE id = ? AND status IN ('done', 'blocked', 'scheduled', 'triage') AND status = ?",
            (new_status, task_id, previous),
        )
        if cur.rowcount != 1:
            return None
        _kb._append_event(conn, task_id, "reopened", {
            "reason": reason[:2000], "actor": actor, "previous_status": "done", "status": new_status,
            **({"stale_row_status": previous} if previous != "done" else {}),
        })
        # add_comment (nested) stamps the ``commented`` provenance: an operator note naming the PR is FYI to
        # active_pr, not a fresh publication that would hold the reopen it records. [daveey]
        _kb.add_comment(conn, task_id, actor, f"REOPENED: {reason}")
        result = _kb.invalidate_descendants_for_parent_reopen(conn, task_id, author=actor)
    for pid, claim_lock, started_at in result["terminations"]:
        _kb._terminate_reclaimed_worker(pid, claim_lock, started_at=started_at)
    _kb.recompute_ready(conn)
    return new_status
