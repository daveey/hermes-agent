"""Separate immutable PR acceptance evidence from later main-health checks."""
from __future__ import annotations

from datetime import datetime


def timestamp(value):
    """Require an unambiguous GitHub timestamp; unknown chronology fails closed."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("GitHub timestamp has no timezone")
    return parsed


def check_receipt(check, sha):
    from hermes_cli.kanban_pr_acceptance import _classify

    is_run = "conclusion" in check
    outcome = check.get("conclusion") if is_run else check["state"]
    return {"name": check.get("name", check.get("context")), "id": check["id"],
            "url": check.get("html_url") or check.get("target_url"),
            "head_sha": check.get("head_sha", check.get("sha")),
            "classification": _classify(check, sha, outcome, is_run), "conclusion": outcome}


def partition_at_merge(checks, merged_at, *, runs):
    """Latest pre-merge execution per producer, plus every later execution.

    App identities stay distinct: an unpinned context must not hide another app's
    failed run behind a success. Legacy statuses have only their context identity.
    """
    cutoff = timestamp(merged_at)
    latest = {}
    post = []
    for check in checks:
        started = timestamp(check.get("started_at") if runs else check.get("created_at"))
        if started > cutoff:
            post.append(check)
            continue
        # A pre-merge start does not make its later conclusion evidence at merge.
        if runs and check.get("completed_at") and timestamp(check["completed_at"]) > cutoff:
            check = {**check, "status": "in_progress", "conclusion": None}
        key = (check["name"], check["app"]["id"]) if runs else check["context"]
        order = (started, check["id"])
        if key not in latest or order > latest[key][0]:
            latest[key] = (order, check)
    return [entry[1] for entry in latest.values()], post


def merge_evidence(receipt, pr, repo, runs, statuses):
    if pr["state"] != "MERGED":
        return runs, statuses
    merged_at = pr["mergedAt"]
    receipt["merged_at"] = merged_at
    runs, post_runs = partition_at_merge(runs, merged_at, runs=True)
    statuses, post_statuses = partition_at_merge(statuses, merged_at, runs=False)
    sha = receipt["head_sha"]
    receipt["post_merge_checks"] = [check_receipt(c, sha) for c in post_runs + post_statuses]
    if any(c["conclusion"] in {"failure", "error", "timed_out", "action_required"}
           for c in receipt["post_merge_checks"]):
        receipt["main_health"] = {
            "idempotency_key": f"ci-red:{repo}:{sha}",
            "detail": f"Main CI is red for {repo}@{sha}; owned by ci-main-watch, not this PR's acceptance."}
    return runs, statuses


def annotate_main_health_owner(conn, receipt):
    """Use the lifecycle's board scope, never open/write a global board implicitly."""
    health = receipt.get("main_health")
    if not health:
        return
    row = conn.execute(
        "SELECT id, status FROM tasks WHERE idempotency_key=? "
        "ORDER BY created_at DESC, id DESC LIMIT 1", (health["idempotency_key"],)).fetchone()
    if row:
        health["owner_card"] = row["id"]
        health["owner_status"] = row["status"]
        health["detail"] += f" Main-health owner card: {row['id']} ({row['status']})."
