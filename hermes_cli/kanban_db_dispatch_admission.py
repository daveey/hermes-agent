"""Pre-claim lane admission for ``_dispatch_lane_task``: rows that must never be claimed here. [daveey]

Both checks run before the per-profile cap, guards, claim and workspace resolution, so a refused row
gets no claim, run, worktree or failure increment (dry runs included). The capsule check runs again
inside the claim transaction (:func:`capsule_claim_check`), so a row, registration or profile that
changes in between is refused the same way.
"""
from __future__ import annotations

import sqlite3


def lane_admission_refused(conn: sqlite3.Connection, task_id: str, assignee: str, result, *,
                           profile_exists, spawn_fn, dry_run: bool) -> bool:
    """True (recorded on ``result``) when the row is not admissible on this dispatcher."""
    from hermes_cli.kanban_capsule_admission import dispatch_refusal

    # Non-profile assignees (control-plane lanes that pull via ``claim_task``)
    # would fail ``hermes -p <assignee>`` at startup and loop ready→crash→ready
    # forever. Bucketed apart from skipped_unassigned: the operator cannot fix
    # it by assigning a profile, and health telemetry suppresses "stuck" for it.
    if profile_exists is not None and not profile_exists(assignee):
        result.skipped_nonspawnable.append(task_id)
        from hermes_cli.kanban_db_dispatch_preclaim import note_nonspawnable
        note_nonspawnable(conn, task_id, assignee, dry_run=dry_run)
        return True
    # Non-local host_repo rows run only through the live registered capsule transport.
    if (code := dispatch_refusal(conn, task_id, assignee, spawn_fn, dry_run=dry_run)) is not None:
        result.capsule_refused.append((task_id, code))
        return True
    # No-progress budget: N runs in a row that only parked again leave the lane (kanban_no_progress). [daveey]
    from hermes_cli.kanban_no_progress import hold as no_progress_hold
    if not dry_run and (held := no_progress_hold(conn, task_id)) is not None:
        result.no_progress_held.append((task_id, held))
        return True
    return False


def capsule_claim_check(conn: sqlite3.Connection, task_id: str, assignee: str, spawn_fn):
    """``(check, refused)``: ``check`` is the write-free recheck ``claim_task`` runs inside its
    transaction; ``refused`` collects its code for :func:`record_claim_refusal` after that transaction."""
    from hermes_cli.kanban_capsule_admission import claim_refusal

    refused: list[str] = []

    def check() -> bool:
        code = claim_refusal(conn, task_id, assignee, spawn_fn)
        if code is not None:
            refused.append(code)
        return code is not None

    return check, refused


def record_claim_refusal(conn: sqlite3.Connection, task_id: str, assignee: str, code: str, result) -> None:
    """After the refused claim's transaction: audit and report, nothing else (no failure increment)."""
    from hermes_cli.kanban_capsule_admission import record_refusal

    record_refusal(conn, task_id, assignee, code)
    result.capsule_refused.append((task_id, code))


def admitted_claim(conn: sqlite3.Connection, task_id: str, assignee: str, lane, ttl_seconds, spawn_fn, result):
    """``claim_if_admitting`` with the capsule recheck inside the claim transaction. A row refused there
    (changed since :func:`lane_admission_refused`) is recorded on ``result`` and returns None, like any lost
    claim; it leaves no claim deferral, so the caller's deferral pop finds none."""
    from hermes_cli.kanban_gateway_drain import claim_if_admitting

    recheck, refusals = capsule_claim_check(conn, task_id, assignee, spawn_fn)
    claimed = claim_if_admitting(conn, task_id, assignee, lane, ttl_seconds, admission_check=recheck)
    if claimed is None and refusals:
        record_claim_refusal(conn, task_id, assignee, refusals[-1], result)
    return claimed
