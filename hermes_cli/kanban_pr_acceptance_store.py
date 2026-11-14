"""Persist acceptance with the same ownership snapshot as the terminal write."""
from __future__ import annotations

from hermes_cli.kanban_db_connect import write_txn
from hermes_cli import kanban_pr_acceptance as _kpa
from hermes_cli.kanban_pr_acceptance import _PR


def _snapshot(conn, task_id):
    row = conn.execute("SELECT current_run_id, status, completion_contract FROM tasks WHERE id=?", (task_id,)).fetchone()
    return tuple(row) if row else None


def malformed_publication_receipt(published_pr) -> dict:
    """Refusal receipt for a ``metadata.published_pr`` that is not one exact GitHub PR URL."""
    return {"ok": False, "classification": "malformed_published_pr", "head_sha": None, "pr_url": None,
            "checks": [], "detail": f"metadata.published_pr must be one exact GitHub PR URL string "
                                    f"(https://github.com/OWNER/REPO/pull/N); got {type(published_pr).__name__}.",
            "recovery": "Retry completion with metadata.published_pr=<exact PR URL> for a PR this card "
                        "published. A review-only card cites the reviewed PR in metadata.pr or "
                        "metadata.reviewed_pr instead; published_pr binds the card's contract permanently."}


def prepare_acceptance(conn, task_id, expected_run_id, metadata):
    snapshot = _snapshot(conn, task_id)
    if snapshot is None:
        return False
    run_id, status, contract = snapshot
    published_pr = metadata.get("published_pr") if isinstance(metadata, dict) else None
    if (not contract or contract == "local-only") and published_pr is None:
        return None
    if status not in {"running", "ready", "blocked", "review"} or (expected_run_id is not None and run_id != expected_run_id):
        return False
    match = _PR.fullmatch(published_pr) if isinstance(published_pr, str) else None
    if published_pr is not None and not match:
        # A shape error is the worker's to fix: never a GitHub read (infra retry) nor a
        # "does not match the persisted contract" claim against a local-only card.
        return snapshot, malformed_publication_receipt(published_pr), metadata
    # Publication binds once, including publications from initially local-only work.
    if match and contract in (None, "local-only", match[1]):
        with write_txn(conn):
            if _snapshot(conn, task_id) != snapshot:
                return False
            conn.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (published_pr, task_id))
        snapshot = (run_id, status, published_pr)
        contract = published_pr
    elif match and published_pr != contract and (bound := _PR.fullmatch(contract)) and bound[1] == match[1]:
        # A closed-unmerged PR may be replaced when the change ships another way.
        # Preserve that recovery path even though closure itself is now a valid
        # disposition. Rebind only on GitHub's positive answer, with an audit event.
        assignee = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()["assignee"]
        if _kpa.pr_closed_unmerged(contract, assignee):
            from hermes_cli.kanban_db import _append_event
            with write_txn(conn):
                if _snapshot(conn, task_id) != snapshot:
                    return False
                conn.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (published_pr, task_id))
                _append_event(conn, task_id, "pr_rebound", {"from": contract, "to": published_pr,
                                                            "reason": "bound PR closed unmerged"}, run_id=run_id)
            snapshot = (run_id, status, published_pr)
            contract = published_pr
    # The assignee profile's gh login owns the repo: acceptance must not run as
    # the ambient login of whichever process completes the card (#122689).
    assignee = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task_id,)).fetchone()["assignee"]
    receipt = _kpa.collect_acceptance(contract, published_pr, assignee=assignee)
    from hermes_cli.kanban_pr_merge_time import annotate_main_health_owner
    annotate_main_health_owner(conn, receipt)
    reason = _kpa.waiver_reason(metadata)
    if reason and receipt.get("classification") == "no_required_checks":
        # The collector proves no required contexts AND no reported CI on THIS
        # fresh read. Failed, pending, missing and unreadable evidence is never waived.
        receipt["waived"] = {"reason": reason}
    from hermes_cli.kanban_pr_landing import check_landing
    check_landing(conn, task_id, receipt, metadata)
    return snapshot, receipt, metadata


def record_acceptance(conn, task_id, acceptance):
    """Called under complete_task's write_txn, before its terminal UPDATE."""
    from hermes_cli.kanban_db import _append_event
    from hermes_cli.kanban_pr_landing import check_landing
    snapshot, receipt, metadata = acceptance
    if _snapshot(conn, task_id) != snapshot:
        return False
    check_landing(conn, task_id, receipt, metadata)
    _append_event(conn, task_id, "pr_acceptance", receipt, run_id=snapshot[0])
    if receipt.get("waived") and receipt.get("classification") == "no_required_checks":
        acceptance_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        _append_event(conn, task_id, "contract_waived",
                      {"reason": receipt["waived"]["reason"], "pr_acceptance_event_id": acceptance_id,
                       "receipt": receipt}, run_id=snapshot[0])
        return acceptance_id
    if not receipt["ok"]:
        detail = f"PR acceptance {receipt['classification']}: {receipt.get('detail', '')} {receipt['recovery']}"
        conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (detail, task_id))
    # Capture the exact core event, not a worker-provided receipt identifier.
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0] if receipt["ok"] else False
