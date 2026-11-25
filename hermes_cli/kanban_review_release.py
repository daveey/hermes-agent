"""Carry a home-evaluated dependency review decision with its promotion.

The receipt is continuation authority, never merge authority. GitHub reads happen
before the promotion transaction; a changed evidence snapshot drops the receipt.
Old/unsupported parks retain the existing local evaluator.
"""
from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_dependency_review as review
from hermes_cli.kanban_db_dispatch_pr_policy import norm_pr
from hermes_cli.kanban_hold_release import _identity, _park


def _snapshot(conn, task_id):
    parents = kb.parent_ids(conn, task_id)
    ids = [task_id, *parents]
    rows = []
    for tid in ids:
        for table in ("tasks", "task_events", "task_runs", "task_comments"):
            key = "id" if table == "tasks" else "task_id"
            rows.append([tuple(r) for r in conn.execute(
                f"SELECT * FROM {table} WHERE {key}=? ORDER BY id", (tid,))])
    try:
        run, _checkpoint, parked = _park(conn, task_id)
    except ValueError:
        return None
    results = [review._completed_review_run(conn, p, parked, run["id"]) for p in parents]
    return parents, kb.parent_states(conn, task_id), rows, [dict(r) if r is not None else None for r in results]


def prepare(conn, failure_limit=None):
    """Precompute only home-owned waiting cards, outside the writer lock.

    Applies ``recompute_ready``'s skips first: a sticky or breaker-held card is
    never promoted this tick, so it gets no GitHub read and no snapshots.
    """
    from hermes_cli.kanban_block_routing import recovery_held
    from hermes_cli.kanban_wake_check import foreign_ids
    from hermes_cli.kanban_home_transfer import frozen
    limit = kb.DEFAULT_FAILURE_LIMIT if failure_limit is None else failure_limit
    copies = foreign_ids(conn)
    prepared = {}
    for row in conn.execute("SELECT id, status, consecutive_failures, max_retries FROM tasks "
                            "WHERE status IN ('todo','blocked')").fetchall():
        tid = row["id"]
        if tid in copies or frozen(conn, tid) or recovery_held(conn, row, limit) or kb.unsatisfied_parents(conn, tid):
            continue
        try:
            _park(conn, tid)
        except ValueError:
            continue
        before = _snapshot(conn, tid)
        stamp = _build(conn, tid)
        if stamp and before == _snapshot(conn, tid):
            prepared[tid] = (before, stamp)
    return prepared


def _build(conn, task_id):
    from hermes_cli.kanban_db_dispatch_pr_state import pr_state, TERMINAL_STATES
    try:
        run, checkpoint, parked = _park(conn, task_id)
    except ValueError:
        return None
    captured = checkpoint.get("prs")
    if not isinstance(captured, list) or not all(isinstance(u, str) for u in captured):
        return None
    urls = {norm_pr(u) for u in captured}
    parents = kb.parent_ids(conn, task_id)
    # Discharge terminal superseded publications at home, not on every executor.
    discharged = {u for u in urls if pr_state(u) in TERMINAL_STATES}
    if not review.resumes_review(conn, task_id, urls, discharged=discharged):
        return None
    proofs = []
    for parent in parents:
        result = review._completed_review_run(conn, parent, parked, run["id"])
        if result is None:
            continue
        data = kb._json_dict(result["metadata"])
        head = next((data[k] for k in ("reviewed_sha", "head", "exact_head")
                     if isinstance(data.get(k), str) and review._SHA.fullmatch(data[k])), None)
        named = review._reviewed_prs(conn, parent, parked, run["id"], urls)
        named |= review._approval_prs(conn, parent, parked, run["id"], urls)
        if head and named:
            proofs.append({"parent": parent, "prs": sorted(named), "head": head,
                           "result": _identity(result), "ended_at": result["ended_at"],
                           "metadata": {k: data[k] for k in (
                               "reviewed_sha", "head", "exact_head", "pr", "reviewed_pr") if k in data}})
    covered = set().union(*(set(p["prs"]) for p in proofs))
    if not urls - discharged or not (urls - discharged).issubset(covered):
        return None
    return {"version": 1, "park": _identity(run), "parents": parents,
            "checkpoint": {k: checkpoint.get(k) for k in ("parents", "awaited", "prs")},
            "prs": sorted(urls), "discharged": sorted(discharged), "reviews": proofs,
            "scope": "one_continuation", "merge_authorized": False}


def promotion_payload(conn, task_id, status, prepared):
    payload = {"status": status} if status != "ready" else {}
    item = prepared.get(task_id)
    if item and item[0] == _snapshot(conn, task_id):
        payload["review_release"] = item[1]
    return payload or None


def released(conn, task_id, pr_urls):
    """Use only a still-unconsumed park and unchanged dependency/PR scope.

    Only a replica (foreign copy) honours the stamp: the home holds every
    piece of evidence, so it keeps re-evaluating ``resumes_review`` locally and
    a post-promotion change to that evidence still re-holds the card there.
    """
    from hermes_cli.kanban_wake_check import foreign_copy
    from hermes_cli.kanban_move_parent_evidence import owner_faulted_since
    if not foreign_copy(conn, task_id):
        return False
    try:
        run, checkpoint, parked = _park(conn, task_id)
    except ValueError:
        return False
    parents = kb.parent_ids(conn, task_id)
    if kb.unsatisfied_parents(conn, task_id) or owner_faulted_since(conn, task_id, parked):
        return False
    event = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='promoted' "
                         "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if event is None:
        return False
    origin = event["origin_gateway"] if "origin_gateway" in event.keys() else None
    home = conn.execute("SELECT created_on FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    # A foreign copy always has a truthy home, so a missing origin differs.
    if origin != home:
        return False
    if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND id>? "
                    "AND kind='descendant_invalidated' LIMIT 1", (task_id, event["id"])).fetchone():
        return False
    stamp = kb._json_dict(event["payload"]).get("review_release")
    if not isinstance(stamp, dict):
        return False
    if (stamp.get("version") != 1 or stamp.get("scope") != "one_continuation"
            or stamp.get("merge_authorized") is not False
            or not review._checkpoint_matches_run(stamp.get("park"), run)
            or "run_uid" not in stamp["park"]
            or stamp.get("parents") != parents
            or stamp.get("checkpoint") != {k: checkpoint.get(k) for k in ("parents", "awaited", "prs")}
            or (kb._to_epoch(event["created_at"]) or 0) < parked):
        return False
    captured = checkpoint.get("prs")
    if not isinstance(captured, list) or not all(isinstance(u, str) for u in captured):
        return False
    urls = {norm_pr(u) for u in captured}
    if stamp.get("prs") != sorted(urls) or not set(map(norm_pr, pr_urls)).issubset(urls):
        return False
    discharged = stamp.get("discharged")
    proofs = stamp.get("reviews")
    if (not isinstance(discharged, list) or not all(isinstance(u, str) for u in discharged)
            or not set(discharged).issubset(urls) or not isinstance(proofs, list)):
        return False
    covered = set()
    for proof in proofs:
        if not isinstance(proof, dict):
            return False
        data = proof.get("metadata")
        named = proof.get("prs")
        head = proof.get("head")
        if (not isinstance(data, dict) or not isinstance(head, str) or not review._SHA.fullmatch(head)
                or head not in [data.get(k) for k in ("reviewed_sha", "head", "exact_head")]
                or proof.get("parent") not in parents or not isinstance(named, list)
                or not all(isinstance(u, str) for u in named) or not set(named).issubset(urls)):
            return False
        latest = conn.execute("SELECT * FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                              (proof["parent"],)).fetchone()
        if latest is not None and not review._checkpoint_matches_run(proof.get("result"), latest):
            return False
        if latest is not None:
            current = kb._json_dict(latest["metadata"])
            # Absent replica evidence is allowed; explicit changed review
            # identity is not. Verdict changes can still authorize repair.
            identity_keys = ("reviewed_sha", "head", "exact_head", "pr", "reviewed_pr")
            if any(k in current and current[k] != data.get(k) for k in identity_keys):
                return False
        explicit = review._named_prs(data, urls)
        if explicit is not None and set(named) != explicit:
            return False
        covered.update(named)
    remaining = urls - set(discharged)
    return bool(remaining) and remaining.issubset(covered)
