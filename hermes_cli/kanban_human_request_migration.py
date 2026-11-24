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
        # A contained card's lifecycle belongs to its containment authority: the Board refuses this generic
        # write (containment_hold), which used to leave a never-applicable rejected upload behind.
        request = validate_human_request(human_request, kind=row["block_kind"])
        from hermes_cli.kanban_containment_guard import pending as contained
        if contained(conn, task_id):  # also a sync quarantine or a pending containment request
            return False
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
            lambda tid: f"cannot classify {tid} (requires a parked card without a current run or a containment hold)")


def reclassify_cli_request(args, reason):
    """Authenticated home-gateway CAS, not a worker or synthetic human answer.

    The supported hub-admin operator transport records the initiating card. The
    enrolled home gateway key authenticates canonical ingress; actor strings and
    null request metadata are not credentials. Never perform a local rekind.
    """
    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_WORKER_TASK"):
        raise PermissionError("classification CAS requires the owner operator surface")
    op = args.classification_cas
    if (not args.metadata_only or args.kind != "dependency" or getattr(args, "human_request", object()) is not None
            or args.ids or args.wake_check or args.check_every is not None or args.clock_only
            or any(getattr(args, name, None) for name in ("needs", "awaiting", "awaiting_why"))
            or not isinstance(op, dict) or op.get("op") != "reclassify_parked"
            or op.get("task") != args.task_id or op.get("reason") != reason):
        raise ValueError("classification CAS requires one exact task, dependency kind, null request and matching reason")
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_sync_pending import pending_task_fields

    ident = client.load_identity()
    hub = client.Hub(ident)
    with cli.kbc.connect_closing() as conn:
        state = sync.sync_state(conn) or {}
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (args.task_id,)).fetchone()
        if (state.get("mode") != "primary" or state.get("gateway_id") != ident.gateway
                or row is None or row["created_on"] != ident.gateway):
            raise PermissionError("classification CAS requires the authenticated primary owner")
        if (row["current_run_id"] is not None or row["claim_lock"] is not None or row["worker_pid"] is not None
                or args.task_id in pending_task_fields(conn)):
            raise ValueError("classification CAS refuses active or locally pending task state")
        board = state["board_id"]
        result = hub.ops(board, [op])
        outcomes = result.get("results")
        if not isinstance(outcomes, list) or len(outcomes) != 1 or outcomes[0].get("status") != "applied":
            raise ValueError(f"classification CAS not applied: {outcomes!r}; preserve the exact request for recovery")
        # Use the ordinary certified feed, not a handwritten local correction.
        client.pull(conn, hub, board, observers=True, partition=ident.gateway)
        canonical = hub.row_evidence(board, "tasks", args.task_id).get("row")
        current = conn.execute("SELECT status,block_kind,human_request,current_run_id,claim_lock,worker_pid FROM tasks WHERE id=?",
                               (args.task_id,)).fetchone()
        expected = {"status": "blocked", "block_kind": "dependency", "human_request": None}
        if (not isinstance(canonical, dict) or any(canonical.get(k) != v for k, v in expected.items())
                or canonical.get("current_run_uid") is not None or canonical.get("lease_holder") is not None
                or current is None or any(current[k] != v for k, v in expected.items())
                or any(current[k] is not None for k in ("current_run_id", "claim_lock", "worker_pid"))):
            raise ValueError("classification receipt recorded but current owner/canonical convergence is unconfirmed; do not resubmit with a new identity")
    print(f"Classified {args.task_id} as dependency (canonical CAS; remains blocked; no execution release)")
    return 0
