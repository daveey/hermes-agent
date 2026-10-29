"""kanban_complete DB reads: own-park detection and the refusal's PR-acceptance cause. [daveey]

A PR-acceptance refusal (no_required_checks, failing, pending or missing checks) used to reach the worker
tagged ``rejection=stale_run``, so digests counted every PR-CI refusal as a stale run. The structured signal is
the ``pr_acceptance`` event (ok=false) that ``record_acceptance`` appends during the refused call: an event id
above the high-water mark taken just before ``complete_task``.
"""

from __future__ import annotations

import json


def parked_by_own_run(conn, tid: str, run_id: int) -> bool:
    """True when ``run_id`` is the card's latest run, ended by ``kanban_block``, and nothing has
    claimed the card since (no current run, no claim lock, status ready/blocked)."""
    row = conn.execute("SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()
    if row is None or row["current_run_id"] is not None or row["claim_lock"] \
            or row["status"] not in ("ready", "blocked"):
        return False
    last = conn.execute("SELECT id, outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                        (tid,)).fetchone()
    return bool(last and last["id"] == run_id and last["outcome"] == "blocked")


def event_mark(conn, tid: str) -> int:
    """The card's highest event id now; events a later call appends have larger ids."""
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]


def pr_acceptance_refused(conn, tid: str, mark: int) -> bool:
    """True when the completion attempt since ``mark`` recorded a failed, unwaived pr_acceptance event."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'pr_acceptance' AND id > ? "
        "ORDER BY id DESC LIMIT 1", (tid, mark)).fetchone()
    if not row:
        return False
    try:
        payload = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is False and not payload.get("waived")
