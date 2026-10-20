"""Checkpoint + continue for Kanban runs that would otherwise die at ``max_runtime_seconds``.

A worker that is not going to finish inside its cap (or is about to sit in a long wait) ends
its run with ``continue_task``: the run closes with outcome ``continued`` and the card goes
straight back to ``ready`` (or to ``scheduled`` with a wake time when it is only waiting), with
the worker's checkpoint rendered at the top of the next run's context. The next run also gets
the b78e9861e6 transcript tail from ``kanban_worker_recovery``.

A continuation is neither a failure nor a block: ``consecutive_failures`` and
``block_recurrences`` are left exactly as they were, so it never trips the give-up breaker or
the unblock-loop detector. Its own bound is ``MAX_CONSECUTIVE_CONTINUATIONS`` trailing
``continued`` runs, after which the worker must complete or block.

The soft deadline itself is worker-side (``tools.kanban_tools.inject_soft_deadline_from_env``):
at ``soft_deadline_fraction`` of the cap the running turn gets an OUT-OF-BAND steer telling it to
checkpoint. The dispatcher's hard kill (``enforce_max_runtime``) is unchanged and still fires at
100% for a worker that ignores it.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional

CONTINUED_OUTCOME = "continued"
CONTINUATION_WOKEN_EVENT = "continuation_woken"
DEFAULT_SOFT_DEADLINE_FRACTION = 0.85
# Bound on back-to-back continuations without a completion (12 x the 4 h default = 48 h).
MAX_CONSECUTIVE_CONTINUATIONS = 12
# A wait handed off with ``resume_after_seconds`` may not park the card longer than this.
MAX_RESUME_AFTER_SECONDS = 24 * 3600
MAX_CHECKPOINT_CHARS = 12000


def soft_deadline_fraction(cfg: Optional[dict] = None) -> float:
    """``kanban.soft_deadline_fraction`` clamped to [0, 0.99]; 0 disables the soft deadline."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    raw = ((cfg or {}).get("kanban") or {}).get("soft_deadline_fraction", DEFAULT_SOFT_DEADLINE_FRACTION)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SOFT_DEADLINE_FRACTION
    return 0.0 if value <= 0 else min(value, 0.99)


def max_consecutive_continuations(cfg: Optional[dict] = None) -> int:
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
    raw = ((cfg or {}).get("kanban") or {}).get("max_continuations", MAX_CONSECUTIVE_CONTINUATIONS)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return MAX_CONSECUTIVE_CONTINUATIONS


def consecutive_continuations(conn: sqlite3.Connection, task_id: str) -> int:
    """Trailing closed runs with outcome ``continued`` (a hard timeout/crash in between does not
    reset the streak; a completed or human-unblocked run does)."""
    rows = conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY id DESC",
        (task_id,),
    ).fetchall()
    count = 0
    for row in rows:
        outcome = row[0]
        if outcome == CONTINUED_OUTCOME:
            count += 1
        elif outcome in ("timed_out", "crashed", "reclaimed", "rate_limited"):
            continue
        else:
            break
    return count


def continue_task(
    conn: sqlite3.Connection, task_id: str, *, checkpoint: str,
    resume_after_seconds: int = 0, expected_run_id: Optional[int] = None,
    max_continuations: Optional[int] = None, metadata: Optional[dict] = None,
) -> dict[str, Any]:
    """End the running run as ``continued`` and re-queue the card.

    ``resume_after_seconds`` 0 -> back to its source phase (``ready``, or ``review`` for a
    reviewer run) at once; > 0 -> ``scheduled`` until ``wake_due_continuations`` promotes it.
    Returns ``{"ok": True, "status", "run_id", "continuation", "wake_at"}`` or
    ``{"ok": False, "error"}``. Never touches ``consecutive_failures`` / ``block_recurrences``.
    """
    from hermes_cli import kanban_db as kb

    checkpoint = (checkpoint or "").strip()
    if not checkpoint:
        return {"ok": False, "error": "checkpoint is required"}
    if len(checkpoint) > MAX_CHECKPOINT_CHARS:
        checkpoint = checkpoint[:MAX_CHECKPOINT_CHARS] + "\n… [checkpoint truncated]"
    try:
        resume_after = int(resume_after_seconds or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "resume_after_seconds must be an integer"}
    if resume_after < 0 or resume_after > MAX_RESUME_AFTER_SECONDS:
        return {"ok": False, "error": f"resume_after_seconds must be between 0 and {MAX_RESUME_AFTER_SECONDS}"}
    limit = max_continuations if max_continuations is not None else max_consecutive_continuations()
    now = int(time.time())
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": f"task {task_id} not found"}
        if row["status"] != "running" or row["current_run_id"] is None:
            return {"ok": False, "error": f"task {task_id} is {row['status']}, not running"}
        if expected_run_id is not None and int(row["current_run_id"]) != int(expected_run_id):
            return {"ok": False, "error": "this run no longer owns the task"}
        streak = consecutive_continuations(conn, task_id)
        if streak >= limit:
            return {"ok": False, "error": (
                f"continuation limit reached ({streak} consecutive continued runs, limit {limit}). "
                "Finish with kanban_complete, or kanban_block with what is stopping progress.")}
        source_status = kb._retry_status_for_run(conn, task_id)
        new_status = "scheduled" if resume_after > 0 else source_status
        wake_at = now + resume_after if resume_after > 0 else None
        cur = conn.execute(
            "UPDATE tasks SET status = ?, claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, worker_started_at = NULL, last_heartbeat_at = NULL "
            "WHERE id = ? AND status = 'running' AND current_run_id = ?",
            (new_status, task_id, int(row["current_run_id"])),
        )
        if cur.rowcount != 1:
            return {"ok": False, "error": "task changed state concurrently"}
        meta = dict(metadata or {})
        from hermes_cli.kanban_shared_blockers import normalize_blocker_id
        if "blocker_id" in meta:
            # Shared external blocker (kanban_shared_blockers); drop an unusable id. [daveey]
            bid = normalize_blocker_id(meta.pop("blocker_id"))
            if bid:
                meta["blocker_id"] = bid
        meta.update({"checkpoint": checkpoint, "continuation": streak + 1,
                     "resume_after_seconds": resume_after, "resume_status": source_status})
        if wake_at is not None:
            meta["wake_at"] = wake_at
        run_id = kb._end_run(
            conn, task_id, outcome=CONTINUED_OUTCOME, status=CONTINUED_OUTCOME,
            summary=kb._first_line(checkpoint, 400) or "checkpoint", metadata=meta,
        )
        payload = {"continuation": streak + 1, "status": new_status, "resume_status": source_status}
        if meta.get("blocker_id"):
            payload["blocker_id"] = meta["blocker_id"]
        if wake_at is not None:
            payload["wake_at"] = wake_at
        kb._append_event(conn, task_id, CONTINUED_OUTCOME, payload, run_id=run_id)
    return {"ok": True, "status": new_status, "run_id": run_id,
            "continuation": streak + 1, "wake_at": wake_at}


# The only event kinds that put a card INTO ``scheduled`` (``continue_task`` with a wait, and
# ``schedule_task``). Every other kind -- comments, heartbeats, notifications, edits, links,
# attachments -- is an annotation on the parked card and must not mask a due wake. [daveey]
SCHEDULED_ENTRY_EVENTS = (CONTINUED_OUTCOME, "scheduled")
# ``wake_due_continuations`` runs every dispatcher tick; a wake this late means it is stuck.
OVERDUE_WAKE_GRACE_SECONDS = 600


def pending_wake_payload(kind: Optional[str], payload: Any) -> Optional[dict]:
    """The parking payload when the event that parked the card is a timed wait, else None: a
    ``continued`` wait, or a ``scheduled`` timed park from ``kanban_timed_park``. [daveey]"""
    from hermes_cli import kanban_db as kb

    if kind not in SCHEDULED_ENTRY_EVENTS:
        return None
    data = payload if isinstance(payload, dict) else kb._json_dict(payload)
    return data if isinstance(data.get("wake_at"), (int, float)) else None


def pending_wake(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    """``continued`` payload (with ``wake_at``) of the event that parked ``task_id`` in ``scheduled``."""
    marks = ",".join("?" for _ in SCHEDULED_ENTRY_EVENTS)
    event = conn.execute(
        f"SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN ({marks}) "
        "ORDER BY id DESC LIMIT 1", (task_id, *SCHEDULED_ENTRY_EVENTS),
    ).fetchone()
    return pending_wake_payload(event[0], event[1]) if event else None


def wake_due_continuations(conn: sqlite3.Connection, *, now: Optional[int] = None) -> list[str]:
    """Promote ``scheduled`` cards parked by a ``continued`` wait whose wake time has passed.

    The parking event is the latest ``SCHEDULED_ENTRY_EVENTS`` row, so a comment or heartbeat
    landing after it does not hide the wake. Cards scheduled by ``kanban_schedule`` (no wake
    time) are left for a human/cron unblock. Counters are untouched (``unblock_task`` would
    reset ``consecutive_failures``).
    """
    from hermes_cli import kanban_db as kb

    now = int(time.time()) if now is None else int(now)
    woken: list[str] = []
    for row in conn.execute("SELECT id FROM tasks WHERE status = 'scheduled'").fetchall():
        tid = row[0]
        payload = pending_wake(conn, tid)
        if payload is None:
            continue
        wake_at = payload["wake_at"]
        if wake_at > now:
            continue
        resume = payload.get("resume_status") if payload.get("resume_status") in ("ready", "review") else "ready"
        with kb.write_txn(conn):
            landing = kb._landing_status_after_parents(conn, tid)
            new_status = "review" if landing == "ready" and resume == "review" else landing
            cur = conn.execute(
                "UPDATE tasks SET status = ?, current_run_id = NULL WHERE id = ? AND status = 'scheduled'",
                (new_status, tid),
            )
            if cur.rowcount == 1:
                kb._append_event(conn, tid, CONTINUATION_WOKEN_EVENT,
                                 {"status": new_status, "wake_at": wake_at})
                woken.append(tid)
    return woken


def render_checkpoint_context(conn: sqlite3.Connection, task_id: str) -> str:
    """The newest ``continued`` checkpoint, if no run has completed since. Placed near the top
    of the worker context so the next run resumes instead of re-orienting from scratch."""
    from hermes_cli import kanban_db as kb

    rows = conn.execute(
        "SELECT id, outcome, metadata, summary FROM task_runs WHERE task_id = ? "
        "AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 20", (task_id,),
    ).fetchall()
    checkpoint_row = None
    later: list[str] = []
    for row in rows:
        if row["outcome"] == CONTINUED_OUTCOME:
            checkpoint_row = row
            break
        if row["outcome"] in ("completed", "review_requested", "changes_requested"):
            return ""
        later.append(f"run {row['id']}: {row['outcome']}")
    if checkpoint_row is None:
        return ""
    meta = kb._json_dict(checkpoint_row["metadata"])
    checkpoint = meta.get("checkpoint") or checkpoint_row["summary"] or ""
    n = meta.get("continuation")
    lines = [
        f"## Continuation checkpoint (from run {checkpoint_row['id']}"
        + (f", continuation #{n}" if n else "") + ")",
        "The previous run of this task hit its runtime budget (or handed off a wait) and wrote this "
        "checkpoint on purpose. Resume from it: verify the listed state (git log/status, files, "
        "`kill -0 <pid>` and log tails for background jobs), re-adopt anything still "
        "running, and continue with the next steps. Do not restart finished steps.",
    ]
    if later:
        lines.append("Runs after the checkpoint (did not checkpoint again): " + ", ".join(reversed(later)) + ".")
    lines += ["", checkpoint.strip(), ""]
    return "\n".join(lines) + "\n"


def soft_deadline_message(*, elapsed: int, limit: int, final: bool) -> str:
    remaining = max(0, limit - elapsed)
    head = ("FINAL WARNING — the dispatcher hard-kills this run in about "
            f"{_fmt(remaining)}." if final else
            f"This run has used {_fmt(elapsed)} of its {_fmt(limit)} max runtime; the dispatcher "
            f"hard-kills it in about {_fmt(remaining)}.")
    return (
        f"[Kanban soft deadline] {head} If the task will not be fully finished (complete / "
        "request_review) within the next few minutes, stop starting new work and call "
        "`kanban_continue(checkpoint=...)` now. The checkpoint must let a fresh run resume without "
        "re-deriving anything: what is done (commits, branches, files, deploys), current state, the "
        "exact next steps, background processes to re-adopt (PIDs and log paths — the next run is a "
        "new process, so process_manage ids do not carry over; jobs must be persist_on_release or "
        "nohup to survive), worktrees, and what must not be redone. If you are only waiting on CI, a "
        "release, a soak or a training checkpoint, pass `resume_after_seconds` instead of polling. A "
        "continuation re-queues the card with your checkpoint and transcript; it is not a failure "
        "or a block. After kanban_continue returns, end your turn with a one-line reply."
    )


def _fmt(seconds: int) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m" + (f"{seconds % 60:02d}s" if seconds % 60 else "")
    return f"{seconds}s"
