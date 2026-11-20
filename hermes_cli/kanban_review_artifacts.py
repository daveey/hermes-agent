"""Artifact-keyed author custody, independent of review verdict/run ordering."""
from __future__ import annotations

import re

from hermes_cli.kanban_review_retry import _mapping, rehandoff_runs


def _identity(metadata):
    """A delivered PR head, whether open or merged; incomplete evidence is unknown."""
    pr, head = metadata.get('published_pr'), metadata.get('reviewed_sha')
    if (not isinstance(pr, str)
            or not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9][0-9]*', pr)
            or not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}', head)):
        return None
    merge = metadata.get('merge_commit')
    if not isinstance(merge, str) or not re.fullmatch(r'[0-9a-f]{40}', merge):
        merge = None
    return pr, head, merge


def _commit_range(metadata, identity):
    """Complete handoff receipt for ``git rev-list merge-base..head``; invalid is unknown.

    The delivering worker supplies this immutable metadata, like reviewed_sha itself.
    Missing receipts never imply disjointness. Producers must list the entire range,
    not only their own commits; the head/base binding prevents stale receipt reuse.
    """
    value = metadata.get('review_commit_range')
    if not isinstance(value, dict) or identity is None:
        return None
    base, head, commits = value.get('base'), value.get('head'), value.get('commits')
    if (head != identity[1] or not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40}', base)
            or not isinstance(commits, list) or not 0 < len(commits) <= 10000
            or any(not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha) for sha in commits)):
        return None
    result = set(commits)
    if head not in result or base in result or len(result) != len(commits):
        return None
    return result


def _related(identity, commits, target, target_commits):
    if identity is None or target is None:
        return True
    if (set(identity[1:]) - {None}) & (set(target[1:]) - {None}):
        return True
    if commits is not None and target_commits is not None:
        return bool(commits & target_commits)
    # No range proof: different heads of a PR can still contain earlier commits.
    return identity[0] == target[0]


def _review_runs(conn, task_id):
    reviewing = set()
    for run_id, kind, raw in conn.execute(
            "SELECT run_id, kind, payload FROM task_events WHERE task_id=? "
            "AND kind IN ('model_tier', 'run_route', 'claimed')", (task_id,)):
        payload = _mapping(raw)
        if payload.get('lane') == 'review' or (kind == 'claimed' and payload.get('source_status') == 'review'):
            reviewing.add(run_id)
    return reviewing


def _unproven_custody(rows, reviewing, target, target_commits):
    """Never narrow across replacement PRs or unaccounted-for range commits.

    The latest handoff attributes its head to its delivering run. Earlier commits
    need earlier delivery evidence, not the latest worker's assertion that they
    are in the diff. A changed PR identity invalidates that attribution context.
    No review verdict alone clears missing delivery evidence.
    """
    if target is None or target_commits is None:
        return True
    covered = {target[1]}
    deliveries = [(rid, _mapping(raw)) for rid, outcome, raw in rows
                  if outcome == 'review_requested' and rid not in reviewing]
    for _, metadata in deliveries[:-1]:
        identity = _identity(metadata)
        if identity is None or identity[0] != target[0]:
            return True
        covered.add(identity[1])
        covered.update(_commit_range(metadata, identity) or ())
    return not target_commits.issubset(covered)


def artifact_author_runs(conn, task_id):
    """Author run ids for the latest artifact, or None for legacy artifact-less cards.

    A verdict never removes artifact custody. Attribute non-review runs to their next
    handoff, retaining checkpoint/crash authors. Identical immutable handoffs retain
    the original delivery's authors even across other artifacts. Without a Git range
    receipt, different heads of the same PR may contain the earlier commits, so retain
    those authors too. This intentionally over-excludes rather than guessing ancestry.
    Unknown deliveries stay conservative. No network or mutable worktree is consulted.
    """
    rows = conn.execute('SELECT id, outcome, metadata FROM task_runs WHERE task_id=? ORDER BY id',
                        (task_id,)).fetchall()
    handoffs = [(rid, _identity(_mapping(raw))) for rid, outcome, raw in rows if outcome == 'review_requested']
    if not any(identity for _, identity in handoffs):
        return None
    target = handoffs[-1][1]
    target_metadata = next(_mapping(raw) for rid, outcome, raw in reversed(rows) if outcome == 'review_requested')
    target_commits = _commit_range(target_metadata, target)
    reviewing = _review_runs(conn, task_id)
    repeats = rehandoff_runs(conn, task_id)
    full_history = {rid for rid, _, _ in rows if rid not in reviewing and rid not in repeats}
    if _unproven_custody(rows, reviewing, target, target_commits):
        return full_history
    pending, authors = set(), set()
    for rid, outcome, raw in rows:
        if rid in reviewing:
            continue
        if rid not in repeats:
            pending.add(rid)
        if outcome != 'review_requested':
            continue
        metadata = _mapping(raw)
        identity = _identity(metadata)
        if _related(identity, _commit_range(metadata, identity), target, target_commits):
            authors.update(pending)
        pending = set()
    return authors | pending
