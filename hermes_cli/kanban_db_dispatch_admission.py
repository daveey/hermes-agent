"""Pre-claim lane admission for ``_dispatch_lane_task``: rows that must never be claimed here. [daveey]

Both checks run before the per-profile cap, guards, claim and workspace resolution, so a refused row
gets no claim, run, worktree or failure increment (dry runs included). The capsule check runs again
inside the claim transaction (:func:`capsule_claim_check`), so a row, registration or profile that
changes in between is refused the same way.
"""
from __future__ import annotations

import sqlite3


def lane_admission_refused(conn: sqlite3.Connection, task_id: str, assignee: str, result, *,
                           profile_exists, spawn_fn, dry_run: bool, lane: str = "ready") -> bool:
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
    # Spend budget: N spending runs (or M cache-read tokens) with no hand-off send the card to triage
    # (kanban_spend_budget). Shares the no_progress_held bucket: both are budget holds. [daveey]
    from hermes_cli.kanban_spend_budget import hold as spend_hold
    if not dry_run and (held := spend_hold(conn, task_id)) is not None:
        result.no_progress_held.append((task_id, f"spend_budget:{held}"))
        return True
    # A fresh card pinned to a quota-walled model would only spawn into the wall (kanban_dispatch_skip). [daveey]
    from hermes_cli.kanban_dispatch_skip import hold_pinned_quota
    return hold_pinned_quota(conn, task_id, result, lane=lane)


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


def _native_local_destination(conn: sqlite3.Connection, task_id: str, spawn_fn) -> bool:
    """True only when this dispatcher will start the worker itself on this host: the native default spawn
    (``spawn_fn`` unset or ``_default_spawn``) and no registered capsule transport for the board. A capsule
    callable, any other custom transport or an unreadable board is NOT a local destination."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_capsule_admission as capsule

    if spawn_fn is not None and spawn_fn is not kbd._default_spawn:
        return False
    db = capsule._conn_db(conn)
    if db is None:
        return False
    with capsule._LOCK:
        handle = capsule._REGISTRY.get(db)
    if handle is not None and handle.live:
        return False  # a registered capsule board: its destination is not proven native
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row is not None and ("runs_on" not in row.keys() or row["runs_on"] in (None, "", "[]"))


def quiet_claim_check(conn: sqlite3.Connection, task_id: str, assignee: str, spawn_fn, result):
    """Write-free refusal closure for an AUTOMATIC native-local claim while a root claim-quiet lease is
    verified active. Runs inside ``claim_task``'s admission transaction; on refusal it records
    ``(task_id, episode_id)`` on ``result.quiet_deferred`` (and the lease deadline) and nothing else.
    Remote/unresolved destinations never refuse. Operator ``claim_task`` never installs this."""

    def check() -> bool:
        from agent.claim_quiet import read_active

        lease = read_active()  # read-only; uncertain/expired/unlocked => None => open
        if lease is None or not _native_local_destination(conn, task_id, spawn_fn):
            return False
        if (task_id, lease.episode_id) not in result.quiet_deferred:
            result.quiet_deferred.append((task_id, lease.episode_id))
        result.quiet_deadlines[lease.episode_id] = lease.expires_at
        return True

    return check


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

    from hermes_cli.kanban_source_board import protocol_call
    capsule_check, refusals = capsule_claim_check(conn, task_id, assignee, spawn_fn)
    quiet_check = quiet_claim_check(conn, task_id, assignee, spawn_fn, result)

    def recheck() -> bool:  # capsule first so its refusal code is never masked by quiet
        return capsule_check() or quiet_check()

    with protocol_call():  # workspace_source R3-a: only the dispatcher's own grant may carry source_protocol
        claimed = claim_if_admitting(conn, task_id, assignee, lane, ttl_seconds, admission_check=recheck)
    if claimed is None and refusals:
        record_claim_refusal(conn, task_id, assignee, refusals[-1], result)
    return claimed
