"""Run-owned dependency review checkpoints for resuming an existing PR.

Neither a promotion nor comment prose is permission to retry an implementation.
Only an authenticated park can capture the PRs and dependency edges being awaited.
A completed review then authorizes one continuation of those same PRs: a repair
when it records ``changes_required`` with ``reviewed_sha``, or a landing
continuation when it records an exact-head approval. The next claim consumes it.
A later comment that repeats a captured PR does not re-arm the hold; a new PR,
a review naming a different PR, or a crash/reclaim does.
"""
from __future__ import annotations

import re


_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_APPROVALS = frozenset({"approve", "approved"})


def checkpoint(conn, task_id, expected_run_id):
    """Capture scope inside block_task's transaction, before closing the owner run."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_URL_RE

    if expected_run_id is None:
        return None
    row = conn.execute(
        "SELECT r.id FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.id = ? AND t.status = 'running' AND r.id = ? "
        "AND r.task_id = t.id AND r.profile = t.assignee AND r.ended_at IS NULL",
        (task_id, expected_run_id),
    ).fetchone()
    if row is None:
        return None
    parents = kb.parent_ids(conn, task_id)
    urls = set()
    for comment in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (task_id,)):
        urls.update(u.lower() for u in _RESPAWN_GUARD_PR_URL_RE.findall(comment["body"] or ""))
    if not parents or not urls:
        return None
    return {"run_id": expected_run_id, "parents": sorted(parents), "prs": sorted(urls)}


def apply_block_update(conn, task_id, expected_run_id, kind, new_status, set_sql, params, payload):
    """Capture review scope and release the claim in the caller's transaction."""
    if kind == "dependency":
        evidence = checkpoint(conn, task_id, expected_run_id)
        if evidence:
            payload["review_checkpoint"] = evidence
    sql = f"""
        UPDATE tasks
           SET status = '{new_status}', claim_lock = NULL, claim_expires = NULL,
               worker_pid = NULL, {set_sql}
         WHERE id = ?
           AND (status IN ('running', 'ready') OR (status = 'review' AND ? = 'assignment'))
    """
    params = (*params, task_id, kind)
    if expected_run_id is not None:
        sql += " AND current_run_id = ?"
        params = (*params, int(expected_run_id))
    return conn.execute(sql, params).rowcount == 1


def _completed_review_run(conn, parent_id, parked_at, owner_run_id):
    """Latest claimed completion of this parent, or None. Never a board comment."""
    from hermes_cli import kanban_db as kb

    row = conn.execute(
        "SELECT r.*, EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = t.id "
        "AND e.run_id = r.id AND e.kind = 'claimed') AS was_claimed "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE t.id = ? AND t.status = 'done' ORDER BY r.id DESC LIMIT 1",
        (parent_id,),
    ).fetchone()
    if (row is None or not row["was_claimed"] or row["outcome"] != "completed"
            or row["id"] == owner_run_id
            or (kb._to_epoch(row["ended_at"]) or 0) < parked_at):
        return None
    return row


def _named_pr(data) -> str | None:
    pr = data.get("pr")
    if isinstance(pr, str) and pr.strip():
        return pr.strip().rstrip("/").lower()
    return None


def _exact_head(data) -> bool:
    return any(
        isinstance(data.get(key), str) and _SHA.fullmatch(data.get(key))
        for key in ("reviewed_sha", "head")
    )


def _reviewed_pr(conn, parent_id, parked_at, owner_run_id):
    """Repair evidence: changes_required plus reviewed_sha and the PR URL."""
    from hermes_cli import kanban_db as kb

    row = _completed_review_run(conn, parent_id, parked_at, owner_run_id)
    if row is None:
        return None
    data = kb._json_dict(row["metadata"])
    sha = data.get("reviewed_sha")
    pr = _named_pr(data)
    if (data.get("verdict") != "changes_required" or not isinstance(sha, str)
            or not _SHA.fullmatch(sha) or pr is None):
        return None
    return pr


def _approval_prs(conn, parent_id, parked_at, owner_run_id, captured: set[str]) -> set[str]:
    """PRs an exact-head approval authorizes, or empty.

    A named ``pr`` is the only PR that approval covers. With no ``pr``, the
    authenticated checkpoint is the binding: the review did not name a
    different head. A short ``head`` is not exact.
    """
    from hermes_cli import kanban_db as kb

    row = _completed_review_run(conn, parent_id, parked_at, owner_run_id)
    if row is None:
        return set()
    data = kb._json_dict(row["metadata"])
    verdict = data.get("verdict")
    verdict_norm = verdict.strip().lower() if isinstance(verdict, str) else ""
    if verdict_norm not in _APPROVALS or not _exact_head(data):
        return set()
    named = _named_pr(data)
    return {named} if named is not None else set(captured)


def resumes_review(conn, task_id, pr_urls):
    """One continuation only: claiming any new run consumes the park's permission."""
    from hermes_cli import kanban_db as kb

    row = conn.execute(
        "SELECT e.payload, e.created_at, r.id, r.profile, r.outcome, t.assignee "
        "FROM task_events e JOIN task_runs r ON r.id = e.run_id "
        "JOIN tasks t ON t.id = e.task_id "
        "WHERE e.task_id = ? AND e.kind = 'dependency_wait' AND r.task_id = t.id "
        "AND r.id = (SELECT MAX(id) FROM task_runs WHERE task_id = t.id) "
        "ORDER BY e.id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if row is None or row["outcome"] != "blocked" or row["profile"] != row["assignee"]:
        return False
    payload = kb._json_dict(row["payload"])
    evidence = payload.get("review_checkpoint")
    if not isinstance(evidence, dict) or evidence.get("run_id") != row["id"]:
        return False
    parents = kb.parent_ids(conn, task_id)
    if (not parents or sorted(parents) != evidence.get("parents")
            or kb.unsatisfied_parents(conn, task_id)):
        return False
    captured = evidence.get("prs")
    if not isinstance(captured, list) or not all(isinstance(u, str) for u in captured):
        return False
    urls = {u.rstrip("/").lower() for u in pr_urls}
    if not urls or not urls.issubset(set(captured)):
        return False
    parked_at = kb._to_epoch(row["created_at"])
    if parked_at is None:
        return False
    reviewed = {_reviewed_pr(conn, p, parked_at, row["id"]) for p in parents}
    if urls.issubset(reviewed):
        return True
    captured_set = {u.rstrip("/").lower() for u in captured}
    # Every parent must authorize. One exact-head approval does not cover a
    # sibling that never reviewed, and a named unrelated PR does not cover
    # the checkpoint set.
    return all(
        urls.issubset(_approval_prs(conn, p, parked_at, row["id"], captured_set))
        for p in parents
    )
