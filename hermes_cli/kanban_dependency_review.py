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
    awaited = [p for p, status in kb.parent_states(conn, task_id)
               if status not in {"done", "archived"}]
    return {"run_id": expected_run_id, "parents": sorted(parents),
            "awaited": sorted(awaited), "prs": sorted(urls)}


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


def _named_prs(data, captured: set[str]) -> set[str] | None:
    """Absent is checkpoint-bound; present must resolve uniquely, or fail closed."""
    if "pr" not in data:
        return None
    pr = data["pr"]
    if type(pr) is int and pr > 0:
        ref = f"#{pr}"
    elif isinstance(pr, str):
        ref = pr.strip().rstrip("/").lower()
    else:
        return set()
    if ref in captured:
        return {ref}
    match = re.fullmatch(r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<num>[1-9][0-9]*)", ref)
    if match is None:
        return set()
    suffix = "/pull/" + match["num"]
    matches = {u for u in captured if u.endswith(suffix) and (
        match["repo"] is None or u == "https://github.com/" + match["repo"] + suffix
    )}
    return matches if len(matches) == 1 else set()


def _exact_head(data) -> bool:
    return any(
        isinstance(data.get(key), str) and _SHA.fullmatch(data.get(key))
        for key in ("reviewed_sha", "head")
    )


_CHANGES_VERDICTS = frozenset({"changes_required", "request_changes", "requested_changes", "changes_requested"})
_SUMMARY_VERDICT = re.compile(r"\bVERDICT:\s*(REQUEST(?:ED)?[_ -]CHANGES|CHANGES[_ -](?:REQUIRED|REQUESTED))\b", re.IGNORECASE)
_SUMMARY_PR_NUM = re.compile(r"\bPR\s*#(\d+)", re.IGNORECASE)


def _requests_changes(row, data) -> bool:
    """True when the completed review's verdict is a change request.

    Reviewers write ``verdict: REQUEST_CHANGES`` (or only ``approved: false`` plus a
    ``VERDICT: REQUEST_CHANGES`` summary line), not the literal ``changes_required``.
    """
    verdict = data.get("verdict")
    if isinstance(verdict, str) and verdict.strip():
        return re.sub(r"[\s-]+", "_", verdict.strip().lower()) in _CHANGES_VERDICTS
    if data.get("approved") is not False:
        return False
    return bool(_SUMMARY_VERDICT.search(row["summary"] or ""))


def _reviewed_prs(conn, parent_id, parked_at, owner_run_id, captured: set[str]) -> set[str]:
    """Repair evidence: a change request at an exact head, bound to one PR.

    A named ``pr`` is the only PR covered. With none, the authenticated checkpoint
    is the binding, unless the summary names ``PR #N`` that matches none of it.
    """
    from hermes_cli import kanban_db as kb

    row = _completed_review_run(conn, parent_id, parked_at, owner_run_id)
    if row is None:
        return set()
    data = kb._json_dict(row["metadata"])
    sha = data.get("reviewed_sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha) or not _requests_changes(row, data):
        return set()
    named = _named_prs(data, captured)
    if named is not None:
        return named
    nums = set(_SUMMARY_PR_NUM.findall(row["summary"] or ""))
    if nums:
        return {u for u in captured if u.rsplit("/", 1)[-1] in nums}
    return set(captured)


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
    named = _named_prs(data, captured)
    return named if named is not None else set(captured)


def _historical_parent(conn, parent_id, parked_at):
    """Legacy checkpoints: exempt only provably finished, unchanged dependencies."""
    from hermes_cli import kanban_db as kb

    row = conn.execute(
        "SELECT r.ended_at, t.status FROM tasks t JOIN task_runs r ON r.task_id = t.id "
        "WHERE t.id = ? ORDER BY r.id DESC LIMIT 1", (parent_id,),
    ).fetchone()
    if row is None or row["status"] not in {"done", "archived"}:
        return False
    ended = kb._to_epoch(row["ended_at"])
    if ended is None or ended >= parked_at:
        return False
    events = conn.execute(
        # Archival and late process-exit receipts do not reopen finished work.
        "SELECT created_at FROM task_events WHERE task_id = ? "
        "AND kind NOT IN ('commented', 'archived', 'worker_result')",
        (parent_id,),
    )
    return all((kb._to_epoch(e["created_at"]) or float("inf")) < parked_at for e in events)


def _awaited_parents(conn, parents, evidence, parked_at):
    if "awaited" not in evidence:
        return [p for p in parents if not _historical_parent(conn, p, parked_at)]
    awaited = evidence["awaited"]
    if (not isinstance(awaited, list) or not awaited or not all(isinstance(p, str) for p in awaited)
            or not set(awaited).issubset(parents) or len(set(awaited)) != len(awaited)):
        return []
    # A historical dependency subsequently reopened is no longer historical.
    return [p for p in parents if p in awaited or not _historical_parent(conn, p, parked_at)]


def resumes_review(conn, task_id, pr_urls, *, discharged=()):
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
    parents = _awaited_parents(conn, parents, evidence, parked_at)
    if not parents:
        return False
    urls -= {u.rstrip("/").lower() for u in discharged}
    if not urls:
        return False
    captured_set = {u.rstrip("/").lower() for u in captured}
    reviewed = set().union(*(_reviewed_prs(conn, p, parked_at, row["id"], captured_set) for p in parents))
    if urls.issubset(reviewed):
        return True
    # Every awaited parent must authorize. One exact-head approval does not cover a
    # sibling that never reviewed, and a named unrelated PR does not cover
    # the checkpoint set.
    return all(
        urls.issubset(_approval_prs(conn, p, parked_at, row["id"], captured_set))
        for p in parents
    )
