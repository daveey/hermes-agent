"""Custody for review admission retries and immutable merged-artifact re-handoffs."""
from __future__ import annotations

import json
import re


def _mapping(raw):
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _artifact(metadata):
    """Only a complete immutable merged-PR identity can prove the same deliverable."""
    pr = metadata.get('published_pr')
    merge, head = metadata.get('merge_commit'), metadata.get('reviewed_sha')
    if (metadata.get('pr_state') != 'MERGED' or not isinstance(pr, str)
            or not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9][0-9]*', pr)
            or any(not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha) for sha in (merge, head))):
        return None
    return pr, merge, head


def rehandoff_runs(conn, task_id):
    """Retain the first author; exempt only repeats of the same immutable deliverable.

    Any intervening executed work without such a handoff resets the evidence. Admission
    failures and synthetic operator parks have no execution receipt and do not reset it.
    Unknown/malformed or changed handoffs are always implementation, never exemptions.
    """
    executed = {row[0] for row in conn.execute(
        "SELECT DISTINCT run_id FROM task_events WHERE task_id=? "
        "AND kind IN ('run_route', 'worker_result', 'spawned', 'owner_admitted')", (task_id,))}
    # A reviewer inspecting the head is not work on the artifact: a verdict between two
    # identical handoffs leaves the same immutable deliverable under review.
    reviewing = set()
    for run_id, kind, raw in conn.execute(
            "SELECT run_id, kind, payload FROM task_events WHERE task_id=? "
            "AND kind IN ('model_tier', 'run_route', 'claimed')", (task_id,)):
        payload = _mapping(raw)
        if payload.get('lane') == 'review' or (kind == 'claimed' and payload.get('source_status') == 'review'):
            reviewing.add(run_id)
    executed -= reviewing
    prior = None
    repeats = set()
    for run_id, outcome, raw in conn.execute(
            'SELECT id, outcome, metadata FROM task_runs WHERE task_id=? ORDER BY id', (task_id,)):
        if outcome == 'review_requested':
            identity = _artifact(_mapping(raw))
            if identity is not None and identity == prior:
                repeats.add(run_id)
            prior = identity
        elif run_id in executed:
            prior = None
    return repeats


def admission_gave_up(conn, task_id, run_id, metadata, error):
    """A refused review spawn is infrastructure, not a failed implementation attempt."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND kind='gave_up' ORDER BY id DESC LIMIT 1",
        (task_id, run_id)).fetchone()
    payload = _mapping(row[0] if row else None)
    if payload.get('admission_refusal') == 'review_model_unavailable':
        return True
    # Historical runs predate the typed gave_up event. Preserve all other failures.
    meta = _mapping(metadata)
    return (meta.get('retry_status') == 'review' and meta.get('trigger_outcome') == 'spawn_failed'
            and isinstance(error, str) and error.startswith('Review model resolution failed: '))
