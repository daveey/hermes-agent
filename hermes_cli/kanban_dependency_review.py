"""Run-owned dependency review checkpoints for resuming an existing PR.

Neither a promotion nor comment prose is permission to retry an implementation.
Only an authenticated park can capture the PRs and dependency edges being awaited.
A completed review then authorizes one continuation of those same PRs: a repair
when it records changes or an absent/unrecognised verdict with an exact
``reviewed_sha``/``head``/``exact_head``, or a landing
continuation when it records an exact-head approval. The next claim consumes it.
A review parent linked after the park by someone else (an orchestrator's
``kanban_link``) counts as awaited only as an exact-current-head re-review of a
captured PR (:mod:`kanban_dependency_review_relink`).
A captured PR a historical parent approved before the park, still at that exact head,
is covered when every awaited parent finished with an exact-head verdict, even on a PR the
checkpoint never captured (:mod:`kanban_dependency_review_historical`).
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
    from hermes_cli.kanban_db_dispatch_pr_policy import norm_pr

    if expected_run_id is None:
        return None
    row = conn.execute(
        "SELECT r.* FROM tasks t JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.id = ? AND t.status = 'running' AND r.id = ? "
        "AND r.task_id = t.id AND r.profile = t.assignee AND r.ended_at IS NULL",
        (task_id, expected_run_id),
    ).fetchone()
    if row is None:
        return None
    parents = kb.parent_ids(conn, task_id)
    urls = set()
    for comment in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (task_id,)):
        urls.update(norm_pr(u) for u in _RESPAWN_GUARD_PR_URL_RE.findall(comment["body"] or ""))
    if not parents or not urls:
        return None
    awaited = [p for p, status in kb.parent_states(conn, task_id)
               if status not in {"done", "archived"}]
    evidence = {"run_id": expected_run_id, "parents": sorted(parents),
                "awaited": sorted(awaited), "prs": sorted(urls)}
    # Integer run IDs are database-local; sync remaps the event FK but deliberately
    # leaves its JSON payload unchanged. Carry the run's stable identity as well.
    if "uid" in row.keys() and row["uid"]:
        evidence["run_uid"] = row["uid"]
    # Server-derived ordering proof for PR comments sharing the park's second (an owner
    # posts its state comment and parks in one turn). Same contract as the continuation
    # checkpoint's comment_id_at_checkpoint: a host-local row id, mapped back on read.
    evidence["comment_id_at_checkpoint"] = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM task_comments WHERE task_id = ?", (task_id,),
    ).fetchone()[0]
    return evidence


def apply_block_update(conn, task_id, expected_run_id, kind, new_status, set_sql, params, payload):
    """Capture review scope and release the claim in the caller's transaction."""
    if kind == "dependency":
        evidence = checkpoint(conn, task_id, expected_run_id)
        if evidence:
            payload["review_checkpoint"] = evidence
    sql = f"""
        UPDATE tasks
           SET status = '{new_status}', claim_lock = NULL, claim_expires = NULL,
               consecutive_failures = CASE WHEN status = 'triage' THEN 0 ELSE consecutive_failures END,
               last_failure_error = CASE WHEN status = 'triage' THEN NULL ELSE last_failure_error END,
               worker_pid = NULL, {set_sql}
         WHERE id = ?
           AND (status IN ('running', 'ready', 'triage') OR (status = 'review' AND ? = 'assignment'))
    """
    params = (*params, task_id, kind)
    if expected_run_id is not None:
        sql += " AND current_run_id = ?"
        params = (*params, int(expected_run_id))
    from hermes_cli.kanban_source_receipt import with_source_receipt
    return with_source_receipt(conn, task_id, lambda: conn.execute(sql, params).rowcount) == 1


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
    if row is None:
        # A checkpointed move can leave the parent's run on another gateway.
        # Use the refreshed Board result, never a move-supplied verdict or a
        # source database's integer run ID as a local identity.
        from hermes_cli.kanban_move_parent_evidence import owner_faulted_since, result
        owner = conn.execute("SELECT task_id FROM task_runs WHERE id = ?", (owner_run_id,)).fetchone()
        if owner and owner_faulted_since(conn, owner[0], parked_at):
            return None  # Neither foreign source may release a crashed/reclaimed owner.
        mirrored = result(conn, owner[0], parent_id) if owner else None
        if (mirrored and mirrored.get("parent_status") == "done" and mirrored.get("was_claimed")
                and mirrored.get("outcome") == "completed"
                and (kb._to_epoch(mirrored.get("ended_at")) or 0) >= parked_at):
            return mirrored
        # A parent created and run on another gateway: its run lives only in the
        # Board replica. Same claimed-completion-after-park rule, Board data only.
        from hermes_cli.kanban_dependency_history import replica_review_run
        return replica_review_run(conn, parent_id, parked_at)
    if (not row["was_claimed"] or row["outcome"] != "completed"
            or row["id"] == owner_run_id
            or (kb._to_epoch(row["ended_at"]) or 0) < parked_at):
        return None
    return row


def _named_prs(data, captured: set[str]) -> set[str] | None:
    """Absent is checkpoint-bound; present must resolve uniquely, or fail closed.

    ``captured`` holds :func:`norm_pr` keys, so a renamed repository's URL or
    ``repo#N`` ref matches its canonical form (and vice versa); never number-only.
    """
    from hermes_cli.kanban_db_dispatch_pr_policy import canonical_repo, norm_pr

    key = "pr" if "pr" in data else "reviewed_pr"
    if key not in data:
        return None
    pr = data[key]
    if type(pr) is int and pr > 0:
        ref = f"#{pr}"
    elif isinstance(pr, str):
        ref = norm_pr(pr)
    else:
        return set()
    if ref in captured:
        return {ref}
    match = re.fullmatch(r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<num>[1-9][0-9]*)", ref)
    if match is None:
        return set()
    suffix = "/pull/" + match["num"]
    repo = canonical_repo(match["repo"]) if match["repo"] else None
    matches = {u for u in captured if u.endswith(suffix) and (
        repo is None or u == "https://github.com/" + repo + suffix
    )}
    return matches if len(matches) == 1 else set()


def _exact_head(data) -> bool:
    return any(
        isinstance(data.get(key), str) and _SHA.fullmatch(data.get(key))
        for key in ("reviewed_sha", "head", "exact_head")
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


def _prefix_repair_head(data) -> bool:
    """Mixed prefix findings permit repair only, never an approval inference.

    Producers can review a PR and its stacked follow-up together. Require a
    complete exact-head/verdict pairing, an explicit change request and named PR;
    canonical single-head handoffs remain the preferred producer format.
    """
    verdicts = data.get("verdicts")
    heads = {key.removeprefix("reviewed_sha_"): value for key, value in data.items()
             if key.startswith("reviewed_sha_prefix")}
    if data.get("verdict") is not None or not isinstance(verdicts, dict):
        return False
    if set(heads) != set(verdicts) or not (data.get("pr") or data.get("reviewed_pr")):
        return False
    if not all(re.fullmatch(r"prefix[1-9][0-9]*", key)
               and isinstance(value, str) and _SHA.fullmatch(value) for key, value in heads.items()):
        return False
    values = list(verdicts.values())
    return (all(isinstance(v, str) and v in {"APPROVE", "REQUEST_CHANGES"} for v in values)
            and "REQUEST_CHANGES" in values)


def _reviewed_prs(conn, parent_id, parked_at, owner_run_id, captured: set[str]) -> set[str]:
    """Repair evidence: changes or an unknown verdict at an exact head, PR-bound.

    A named ``pr`` (or ``reviewed_pr`` fallback) is the only PR covered. With none, the authenticated checkpoint
    is the binding, unless the summary names ``PR #N`` that matches none of it.
    """
    from hermes_cli import kanban_db as kb

    row = _completed_review_run(conn, parent_id, parked_at, owner_run_id)
    if row is None:
        return set()
    data = kb._json_dict(row["metadata"])
    verdict = data.get("verdict")
    approval = isinstance(verdict, str) and verdict.strip().lower() in _APPROVALS
    # Respawn is not merge approval. An unrecognised/absent verdict still hands
    # findings back to the parked owner; keep explicit approvals on their all-parent path.
    exact_repair = _exact_head(data) or _prefix_repair_head(data)
    if not exact_repair or (approval and not _requests_changes(row, data)):
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

    A named ``pr`` (or ``reviewed_pr`` fallback) is the only PR that approval
    covers. With neither key, the authenticated checkpoint supplies the PR scope.
    A short ``head`` is not exact.
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


_REVIEW_KEYS = ("verdict", "approved", "reviewed_sha")
# Keys a result can carry without its worker describing any work: a PR reference, the
# session id the worker tools (kanban_complete, kanban_continue, kanban_request_review)
# stamp on a dispatcher worker's own result, and artifacts (files the worker lists, by
# artifacts= or by hand, plus scratch-workspace files complete_task finds named in the
# summary or result). Any other key routes by _REVIEW_KEYS alone, so hub reviews that
# record review_outcome or reviewer_checks, and size-judge rulings, take the ordering
# continuation as they did before #728. [daveey]
_LOST_REVIEW_KEYS = frozenset({"pr", "reviewed_pr", "worker_session_id", "artifacts", "artifact_receipt"})


def is_review_result(metadata, summary, captured) -> bool:
    """True when a parent's result belongs to the review rules, not the ordering handoff.

    Verdict keys or a ``VERDICT:`` line mark a review (a bare ``head`` does not:
    operator gates record one). So does a result with only ``_LOST_REVIEW_KEYS`` that
    names a checkpointed PR (``pr``/``reviewed_pr`` key, summary ``PR #N`` or URL): it may
    be a review whose verdict was lost, so it fails closed on the exact-head check rather
    than passing as an ordering prerequisite once the park is a second past the PR.
    This fallback is a heuristic both ways: a prerequisite that reports its work only in
    prose and names the PR is held too, and a review that keeps its outcome under other
    keys is treated as a prerequisite. [daveey]
    """
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_URL_RE
    from hermes_cli.kanban_db_dispatch_pr_policy import norm_pr

    if (any(key in metadata for key in _REVIEW_KEYS)
            or any(key.startswith("reviewed_sha_prefix") for key in metadata)
            or _SUMMARY_VERDICT.search(summary or "")):
        return True
    prs = {norm_pr(u) for u in captured or () if isinstance(u, str)}
    if not prs or not set(metadata) <= _LOST_REVIEW_KEYS:
        return False
    if _named_prs(metadata, prs):
        return True
    text = (summary or "").lower()
    nums = set(_SUMMARY_PR_NUM.findall(text))
    named = {norm_pr(u) for u in _RESPAWN_GUARD_PR_URL_RE.findall(text)}
    return any(u in text or u in named or u.rsplit("/", 1)[-1] in nums for u in prs)


def _historical_parent(conn, parent_id, parked_at):
    """Use local custody first; eviction may require verified Board history."""
    if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (parent_id,)).fetchone() is not None:
        return _historical_parent_history(conn, parent_id, parked_at)
    from hermes_cli.kanban_dependency_history import replica_historical_parent
    return replica_historical_parent(conn, parent_id, parked_at)


def _historical_parent_history(conn, parent_id, parked_at):
    """Exempt only provably finished, unchanged dependencies in one snapshot."""
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
        # Card/workspace archival, late process-exit receipts and comment wake snapshots
        # do not reopen finished work. Keep this explicit: unknown events fail closed.
        "SELECT created_at FROM task_events WHERE task_id = ? "
        "AND kind NOT IN ('commented', 'archived', 'workspace_archived', "
        "'worker_result', 'wake_episode_evidence')",
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


def _checkpoint_matches_run(evidence, run):
    if not isinstance(evidence, dict):
        return False
    if "run_uid" in evidence:
        uid = evidence["run_uid"]
        # A present but invalid/mismatched UID must never fall back to an integer
        # that happens to collide on this gateway.
        return isinstance(uid, str) and bool(uid) and "uid" in run.keys() and uid == run["uid"]
    # Legacy/local-only boards have no UID. Do not infer a remapped legacy ID.
    return type(evidence.get("run_id")) is int and evidence["run_id"] == run["id"]


def resumes_review(conn, task_id, pr_urls, *, discharged=()):
    """One continuation only: claiming any new run consumes the park's permission
    (a hermes-sync core-update requeue does not: :mod:`kanban_core_update_requeue`)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_core_update_requeue import latest_run_skipping_requeues
    from hermes_cli.kanban_db_dispatch_pr_policy import norm_pr

    # The park must be the card's latest run that executed. Passed over: a core-update
    # requeue (that pause is not the consuming claim; 2026-11-12: t_01a1ce8aff1f09c1 held
    # after an exact-head APPROVE) and a Board grant returned unused (it ran nothing). [daveey]
    latest = latest_run_skipping_requeues(conn, task_id)
    row = conn.execute(
        "SELECT e.payload, e.created_at, r.*, t.assignee "
        "FROM task_events e JOIN task_runs r ON r.id = e.run_id "
        "JOIN tasks t ON t.id = e.task_id "
        "WHERE e.task_id = ? AND e.kind = 'dependency_wait' AND r.task_id = t.id "
        "AND r.id = ? ORDER BY e.id DESC LIMIT 1", (task_id, latest["id"]),
    ).fetchone() if latest is not None else None
    if row is None or row["outcome"] != "blocked" or row["profile"] != row["assignee"]:
        return False
    payload = kb._json_dict(row["payload"])
    evidence = payload.get("review_checkpoint")
    if not isinstance(evidence, dict) or not _checkpoint_matches_run(evidence, row):
        return False
    parents = kb.parent_ids(conn, task_id)
    checkpointed = evidence.get("parents")
    if (not parents or not isinstance(checkpointed, list) or kb.unsatisfied_parents(conn, task_id)
            or [p for p in parents if p in checkpointed] != checkpointed):
        return False
    captured = evidence.get("prs")
    if not isinstance(captured, list) or not all(isinstance(u, str) for u in captured):
        return False
    captured_set = {norm_pr(u) for u in captured}
    urls = {norm_pr(u) for u in pr_urls}
    if not urls or not urls.issubset(captured_set):
        return False
    parked_at = kb._to_epoch(row["created_at"])
    if parked_at is None:
        return False
    # A parent the checkpoint never saw counts as awaited only as an exact-current-head
    # re-review of a captured PR, linked after the park (an orchestrator's kanban_link).
    late = {p for p in parents if p not in checkpointed}
    if late:
        from hermes_cli.kanban_dependency_review_relink import late_review_parents
        if not late_review_parents(conn, task_id, late, parked_at, row["id"], captured_set):
            return False
    parents = _awaited_parents(conn, parents, evidence, parked_at)
    if not parents:
        return False
    urls -= {norm_pr(u) for u in discharged}
    if not urls:
        return False
    repairs = {p: _reviewed_prs(conn, p, parked_at, row["id"], captured_set) for p in parents}
    reviewed = set().union(*repairs.values())
    if urls.issubset(reviewed):
        return True
    # Without repair evidence, every awaited parent must approve a checkpointed PR. One approval
    # does not cover a sibling that never reviewed, and a named unrelated PR approves nothing.
    # A split review (one card per PR of a multi-repo change, each naming its own reviewed_pr)
    # is covered by the union, but only among parents the checkpoint itself named as awaited.
    # A parent that is here only because its history could not be proven finished before the
    # park (legacy checkpoint, reopened or replica-unverified) must approve every holding PR on
    # its own: its pre-park approval of another PR is not evidence for this one.
    # Mixed split reviews (one named parent requests changes on its PR, a sibling approves
    # the other) also resume: each named parent left exact-head evidence for a checkpointed
    # PR and together they cover every holding PR. Still no merge approval, one continuation.
    approvals = {p: _approval_prs(conn, p, parked_at, row["id"], captured_set) for p in parents}
    named_awaited = evidence.get("awaited")
    named = [*(named_awaited if isinstance(named_awaited, list) else ()), *late]
    covered = reviewed.union(*approvals.values())
    if (all(approvals[p] or repairs[p] for p in parents)
            and all(urls.issubset(a) for p, a in approvals.items() if p not in named)
            and urls.issubset(covered)):
        return True
    # A captured PR a historical parent approved before the park, still at that head, while the
    # awaited parents reviewed (exact head, explicit verdict) a sibling PR the checkpoint never
    # captured (:mod:`kanban_dependency_review_historical`; 2026-11-13 t_01a1d4635554fa12).
    from hermes_cli.kanban_dependency_review_historical import covers
    return covers(conn, parents=parents, checkpointed=checkpointed, named_awaited=named, urls=urls,
                  covered=covered, parked_at=parked_at, owner_run_id=row["id"], captured=captured_set)
