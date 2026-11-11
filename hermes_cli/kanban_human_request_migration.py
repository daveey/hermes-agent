"""Explicit, audited classification of legacy parked cards; never unblock them."""
import os

from hermes_cli.kanban_human_request import validate_human_request


def classify_parked_request(conn, task_id: str, *, human_request: dict | None,
                            actor: str, reason: str) -> bool:
    """Attach reviewed evidence without changing status, runs or block counters.

    Callers must use their normal board write authorization. The process-env
    check below is only a local CLI/worker guard, not HTTP caller identity.
    Dashboard routes use web_server._require_token before entering this helper:
    verified dashboard sessions or the shared loopback token are trusted operators,
    with no worker/operator role distinction for holders of those credentials.
    Actor is audit provenance, never an authorization credential. Classification
    neither grants approval nor authenticates an OS user. No inference from kind,
    body, title or authentication failure is safe during migration.
    """
    from hermes_cli import kanban_db as db

    if os.environ.get("HERMES_KANBAN_TASK"):
        raise PermissionError("legacy classification is orchestrator-only")
    if not isinstance(actor, str) or not actor.strip() or not reason.strip():
        raise ValueError("classification requires actor and reason")
    # Metadata/event writes have no post-commit effects: compose atomically with
    # the caller's eligibility check and audit comment (outer rollback owns both).
    with db.write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT status, block_kind, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["status"] not in ("blocked", "triage") or row["current_run_id"] is not None:
            return False
        request = validate_human_request(human_request, kind=row["block_kind"])
        from hermes_cli.kanban_ask_resolved import filing_fields, store_request
        store_request(conn, task_id, request)  # adds, changes or removes the resolved_check probe [daveey]
        db._append_event(conn, task_id, "blocked", {
            "human_request": request, "kind": row["block_kind"], "reason": reason,
            "actor": actor, "classified_in_place": True, "metadata_only": True,
            **(filing_fields(conn, request) if row["status"] == "blocked" else {}),
        })
        return True


def classify_cli_request(args, request, reason):
    """Explicit CLI metadata-only mode, separate from the block lifecycle."""
    from hermes_cli import kanban as cli
    from hermes_cli.kanban_cli_actor import cli_actor

    if not hasattr(args, "human_request") or not reason:
        raise ValueError("metadata-only requires --human-request and a reason")
    if args.kind or args.wake_check or args.check_every is not None or args.clock_only:
        raise ValueError("metadata-only cannot change kind or wake settings")
    with cli.kbc.connect_closing() as conn:
        return cli._bulk_apply(
            cli._bulk_ids(args),
            lambda tid: classify_parked_request(conn, tid, human_request=request,
                                               actor=cli_actor(), reason=reason),
            lambda tid: f"Classified {tid} (metadata only; remains parked)",
            lambda tid: f"cannot classify {tid} (requires parked card without a current run)")
