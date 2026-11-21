"""Pre-claim checks of one dispatch candidate (split out of ``kanban_db_dispatch``, file cap). [daveey]"""
from __future__ import annotations

import sqlite3


def resolve_default_assignee(default_assignee, profile_exists_fn):
    """Resolve the configured default only when this home's profile allowlist admits it.

    Unimportable profiles trust config only without an allowlist; the downstream
    spawn gate still classifies missing profiles as nonspawnable.
    """
    name = (default_assignee or "").strip() or None
    if name:
        profile_exists = profile_exists_fn()
        if profile_exists is not None and not profile_exists(name):
            return None
    return name


def note_nonspawnable(conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool) -> None:
    """Per-task diagnostic so ``show``/``tail`` name the missing profile instead of leaving the card in
    ``ready`` with zero board evidence (#122422). Unlike a respawn guard the condition never expires on
    its own, so write it once: a repeat only when something else happened on the card since (reassign,
    comment) — not one row per tick forever, and not one row per foreign home per tick on a shared
    board (#101015)."""
    if dry_run:
        return
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        last = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind != 'dispatch_skipped' "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
        if (last is None or last["kind"] != "skipped_nonspawnable"
                or last["payload"] != kb._json_or_null({"assignee": assignee})):
            kb._append_event(conn, task_id, "skipped_nonspawnable", {"assignee": assignee})


def eligible_rows(conn: sqlite3.Connection, rows: list[sqlite3.Row], status: str,
                   result) -> list[sqlite3.Row]:
    from hermes_cli.kanban_worker_node import dispatch_refusals
    refused = dispatch_refusals(conn, status) if rows else {}
    if not refused:
        return rows
    from hermes_cli import kanban_repin_notice as repin
    from hermes_cli.kanban_worker_placement import WAITING
    kept = []
    for row in rows:
        reason = refused.get(row["id"])
        if reason is None:
            kept.append(row)
        else:
            if reason == WAITING and repin.awaiting(conn, row["id"]):
                from hermes_cli.kanban_repin_owner import wait_reason
                reason = wait_reason(conn, row["id"])
            result.skipped_ineligible.append((row["id"], reason))
            from hermes_cli.kanban_candidate_receipts import note
            note(row["id"], "labels" if reason == "labels_mismatch" else "prefilter", reason,
                 stage="eligibility", status=status)
            if reason == "labels_mismatch" and getattr(result, "scheduling_admitted", False):
                note_unplaceable(conn, row["id"])
    return kept


def hold_ineligible_budgets(conn: sqlite3.Connection, result, *, dry_run: bool) -> None:
    """Placement is an execution filter, not a home-board budget exemption."""
    from hermes_cli.kanban_db_dispatch_admission import budget_refused

    for task_id, _reason in result.skipped_ineligible:
        budget_refused(conn, task_id, result, dry_run=dry_run)


def note_unplaceable(conn: sqlite3.Connection, task_id: str) -> None:
    """One ``skipped_unplaceable`` event when no known machine satisfies the card's ``runs_on``: the skip was in
    the tick result only, so t_01a1a521638d0a78 (runs_on ["hermes-hub"]) sat ready for days with nothing on the
    card saying why. Written again only when the missing labels change or something else happened since. [daveey]"""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_queue_position as kqp
    try:
        place = kqp.placement(conn, task_id, backlog=False)
    except Exception:  # health: allow BLE001 -- the event is advisory; the skip itself already happened
        kb._log.debug("kanban dispatch: unplaceable check failed for %s", task_id, exc_info=True)
        return
    if not place or place.get("lane") != "unplaceable":
        return
    payload = {"runs_on": place["runs_on"], "missing": place["missing"], "machines": place["machines"]}
    with kb.write_txn(conn):
        last = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind != 'dispatch_skipped' "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
        if last is None or last["kind"] != "skipped_unplaceable" or last["payload"] != kb._json_or_null(payload):
            kb._append_event(conn, task_id, "skipped_unplaceable", payload)
