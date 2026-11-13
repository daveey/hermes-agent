"""A pre-park exact-head approval of a captured PR still at that head covers it. [daveey]

Production shape (2026-11-13, t_01a1d4635554fa12): the owner published core PR #2097 and
state PR #2108 and filed one review card per PR. Review R1 approved #2097 at its exact head
BEFORE the owner parked; the owner then parked on ``dependency_wait`` with R2 (the #2108
review) still awaited, and captured only #2097. R2 approved #2108 at its exact head. The
owner stayed ``active_pr``-held: R1 is historical (finished before the park), so it covers
nothing, and R2 names a PR the checkpoint never captured, so it covers nothing either.

:func:`covers` grants that one continuation only when all of these hold (the caller,
:func:`kanban_dependency_review.resumes_review`, has already applied every checkpoint,
run-binding, parent-set and crash/reclaim check):

* every awaited parent is one the checkpoint itself named as awaited (or an accepted late
  re-review); a parent awaited only because its history could not be proven finished
  before the park keeps the old per-parent rule;
* every awaited parent completed (claimed, after the park) with an explicit verdict
  (approve or a change request) at a full exact head. A PR it names that the checkpoint
  did not capture is ordering proof only, never coverage for a captured PR;
* every holding PR that no awaited parent covers is covered by a checkpointed parent that
  is historical (``_historical_parent``: finished before the park, unchanged since, on
  this board) whose latest claimed completion approves THAT PR by name (``pr`` /
  ``reviewed_pr``; an absent name binds nothing here) at a full ``reviewed_sha`` equal to
  the PR's current GitHub head, confirmed by a fresh read (:func:`kanban_awaited_review._at_head`).

A moved head, a gh failure, a change request or a missing/short head keeps the hold. It
never authorizes a merge; the next claim consumes the continuation.
"""
from __future__ import annotations


def _explicit_verdict_at_head(conn, parent_id, parked_at, owner_run_id) -> bool:
    """True when the parent completed after the park with approve/changes at a full head."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_dependency_review as review

    row = review._completed_review_run(conn, parent_id, parked_at, owner_run_id)
    if row is None:
        return False
    data = kb._json_dict(row["metadata"])
    verdict = data.get("verdict")
    approve = isinstance(verdict, str) and verdict.strip().lower() in review._APPROVALS
    if not (approve or review._requests_changes(row, data)):
        return False
    return review._exact_head(data)


def _historical_approval(conn, parent_id, parked_at, captured: set[str]):
    """(named PRs, reviewed_sha) of a historical parent's approval, or None."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_dependency_review as review

    if not review._historical_parent(conn, parent_id, parked_at):
        return None
    row = conn.execute(
        "SELECT r.*, EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = r.task_id "
        "AND e.run_id = r.id AND e.kind = 'claimed') AS was_claimed "
        "FROM task_runs r WHERE r.task_id = ? ORDER BY r.id DESC LIMIT 1", (parent_id,),
    ).fetchone()
    if row is None or not row["was_claimed"] or row["outcome"] != "completed":
        return None  # replica-only history has no local run row: fail closed
    data = kb._json_dict(row["metadata"])
    verdict = data.get("verdict")
    sha = data.get("reviewed_sha")
    if (not isinstance(verdict, str) or verdict.strip().lower() not in review._APPROVALS
            or data.get("approved") is False
            or not isinstance(sha, str) or not review._SHA.fullmatch(sha)):
        return None
    named = review._named_prs(data, captured)
    if not named:
        return None  # the checkpoint was taken after this review: it binds no scope here
    return named, sha.lower()


def covers(conn, *, parents, checkpointed, named_awaited, urls, covered,
           parked_at, owner_run_id, captured: set[str]) -> bool:
    """One continuation when awaited parents gave ordering proof and history covers the rest.

    ``parents`` are the awaited parents; ``covered`` the holding PRs their own exact-head
    evidence already covers (repair or approval of a captured PR).
    """
    if not parents or not all(p in named_awaited for p in parents):
        return False
    if not all(_explicit_verdict_at_head(conn, p, parked_at, owner_run_id) for p in parents):
        return False
    missing = set(urls) - set(covered)
    if not missing:
        return False  # nothing for history to cover: the caller's rule already decided
    approvals = []
    for parent in checkpointed:  # an awaited parent finished after the park: never historical
        found = _historical_approval(conn, parent, parked_at, captured)
        if found is not None:
            approvals.append(found)
    if not all(any(url in named for named, _ in approvals) for url in missing):
        return False  # cheap refusal before any gh read
    from hermes_cli.kanban_awaited_review import _at_head
    return all(any(url in named and _at_head(url, sha) for named, sha in approvals)
               for url in sorted(missing))
