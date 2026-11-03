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
    landed = pr.get("mergeCommit")
    if (isinstance(landed, dict) and landed.get("oid") == sha
            and any(c["conclusion"] in {"failure", "error", "timed_out", "action_required"}
                    for c in receipt["post_merge_checks"])):
        branch = pr["baseRefName"]
        label = repo if branch == "main" else f"{repo}@{branch}"
        receipt["main_health"] = {
            "idempotency_key": f"ci-red:{label}:{sha}",
            "detail": f"Base branch {branch} CI is red for {repo}@{sha}; "
                      "ci-main-watch owns base health, not this PR's acceptance."}
    return runs, statuses


def _health_owner_rows(conn, exact_key):
    # ci-main-watch keys a continuing episode by FIRST red SHA, not current SHA.
    # Off main its producer label is repo@branch. Match the whole label, not LIKE
    # (repo/branch names may contain SQL wildcard characters).
    prefix = exact_key.rsplit(":", 1)[0] + ":"
    return conn.execute(
        "SELECT id, status, idempotency_key, created_at FROM tasks "
        "WHERE created_by='ci-main-watch' "
        "AND status IN ('triage','todo','scheduled','ready','running','blocked','review') "
        "AND substr(idempotency_key,1,?)=? "
        "ORDER BY created_at DESC, id DESC", (len(prefix), prefix)).fetchall()


def annotate_main_health_owner(conn, receipt):
    """Diagnostic only: discover existing owners across boards without creating any.

    Other board handles are mode=ro (never the initializing/migrating connect()).
    Missing, old-schema or unreadable boards cannot change acceptance or prevent
    its receipt from being persisted. No profile switching or global mutations.
    """
    import re
    import sqlite3
    from hermes_cli import kanban_lockguard
    from hermes_cli.kanban_db_workspace import _sibling_board_db_files

    health = receipt.get("main_health")
    if not health:
        return
    key = health["idempotency_key"]
    rows = []
    try:
        rows.extend(_health_owner_rows(conn, key))
        paths = _sibling_board_db_files(conn)
    except (OSError, sqlite3.Error):
        paths = []
    for path in paths:
        try:
            other = kanban_lockguard.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
            try:
                other.row_factory = sqlite3.Row
                rows.extend(_health_owner_rows(other, key))
            finally:
                other.close()
        except (OSError, sqlite3.Error):
            continue
    rows = [row for row in rows if re.fullmatch(r"[0-9a-f]{40}", row["idempotency_key"].rsplit(":", 1)[-1])]
    if not rows:
        return
    row = max(rows, key=lambda r: (r["idempotency_key"] == key, r["created_at"], r["id"]))
    health["owner_card"] = row["id"]
    health["owner_status"] = row["status"]
    health["detail"] += f" Main-health owner card: {row['id']} ({row['status']})."
