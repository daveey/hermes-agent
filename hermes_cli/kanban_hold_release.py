"""Machine-checked owner-wake recovery, scoped to one dependency park. [daveey]

This is NOT an unblock or merge approval. Only the authenticated tool grant calls
release; a new run consumes the receipt even if that run crashes (a hermes-sync
core-update restart-safe requeue does not: :mod:`kanban_core_update_requeue`). Generic handoff
and recent-success guards deliberately do not recognize this event.
"""
from __future__ import annotations

import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_dependency_review as review
from hermes_cli.kanban_db_dispatch_pr_policy import norm_pr


def _identity(row):
    return {"run_uid": row["uid"]} if "uid" in row.keys() and row["uid"] else {"run_id": row["id"]}


def _park(conn, task_id):
    from hermes_cli.kanban_core_update_requeue import latest_run_skipping_requeues
    # Latest run that executed: a core-update requeue is a pause, not a newer run, and an
    # unused Board grant ran nothing. [daveey]
    latest = latest_run_skipping_requeues(conn, task_id)
    row = conn.execute(
        "SELECT e.payload, e.created_at, r.*, t.assignee FROM task_events e "
        "JOIN task_runs r ON r.id=e.run_id JOIN tasks t ON t.id=e.task_id "
        "WHERE e.task_id=? AND e.kind='dependency_wait' AND r.task_id=t.id "
        "AND r.id=? ORDER BY e.id DESC LIMIT 1", (task_id, latest["id"]),
    ).fetchone() if latest is not None else None
    if row is None or row["outcome"] != "blocked" or row["profile"] != row["assignee"]:
        raise ValueError("hold release requires latest orderly owner park")
    payload = kb._json_dict(row["payload"])
    evidence = payload.get("review_checkpoint")
    if (not review._checkpoint_matches_run(evidence, row)
            or payload.get("kind") != "dependency"
            or payload.get("source_status") not in {"ready", "review"}):
        raise ValueError("hold release requires authenticated dependency checkpoint")
    parked = kb._to_epoch(row["created_at"])
    if parked is None or row["ended_at"] is None:
        raise ValueError("hold release requires portable completed park identity")
    if not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind='claimed'",
                        (task_id, row["id"])).fetchone():
        raise ValueError("hold release requires claimed owner run")
    return row, evidence, parked


def _scope(conn, task_id):
    row, evidence, parked = _park(conn, task_id)
    parents = kb.parent_ids(conn, task_id)
    original = evidence.get("parents")
    if (not isinstance(original, list) or not original
            or not all(isinstance(p, str) for p in original)
            or not set(original).issubset(parents) or kb.unsatisfied_parents(conn, task_id)):
        raise ValueError("hold release requires retained satisfied dependencies")
    captured = evidence.get("prs")
    if (not isinstance(captured, list) or not captured
            or not all(isinstance(u, str) and u.startswith("https://github.com/") for u in captured)):
        raise ValueError("hold release requires captured PR scope")
    captured = {norm_pr(u) for u in captured}
    covered = set()
    proofs = []
    for parent in parents:
        result = review._completed_review_run(conn, parent, parked, row["id"])
        if result is None:
            continue
        data = kb._json_dict(result["metadata"])
        named = review._named_prs(data, captured)
        if not named or not review._exact_head(data):
            continue
        covered.update(named)
        proofs.append({"task": parent, **_identity(result), "metadata": {
            k: data[k] for k in ("pr", "reviewed_pr", "reviewed_sha", "head", "exact_head") if k in data}})
    if not captured.issubset(covered):
        raise ValueError("hold release requires independent explicit-PR exact-head review after park")
    return row, captured, proofs


def _check_publications(conn, task_id, captured):
    from hermes_cli.kanban_db_dispatch import _RESPAWN_GUARD_PR_URL_RE
    from hermes_cli.kanban_db_dispatch_pr_state import _foreign_pr_comment_ids
    foreign = _foreign_pr_comment_ids(conn, task_id)
    for comment in conn.execute("SELECT * FROM task_comments WHERE task_id=?", (task_id,)):
        if comment["id"] in foreign or comment["author"] == "kanban-dispatcher":
            continue
        urls = {norm_pr(u) for u in _RESPAWN_GUARD_PR_URL_RE.findall(comment["body"] or "")}
        if not urls.issubset(captured):
            raise ValueError("hold release PR publication scope changed")
        at = kb._to_epoch(comment["created_at"])
        if urls and (at is None or at >= int(time.time())):
            raise ValueError("hold release must strictly follow PR publication")


def _event_tip(conn, task_id):
    return conn.execute("SELECT COALESCE(MAX(id),0) FROM task_events WHERE task_id=?", (task_id,)).fetchone()[0]


def release(conn, task_id, *, supervisor, supervisor_board, precondition):
    """Called only with a live exact-target tool grant; never via generic unblock.

    Network-capable admission runs before the write transaction. Its event tip
    must be unchanged inside the transaction; all local eligibility is rechecked.
    """
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_db_dispatch import check_respawn_guard
    from hermes_cli.kanban_wake_check import foreign_copy

    from hermes_cli.kanban_home_transfer import frozen
    if frozen(conn, task_id):
        raise ValueError("home transfer owns this card; hold release deferred")
    tip = _event_tip(conn, task_id)
    if check_respawn_guard(conn, task_id) != "active_pr":
        raise ValueError("hold release requires active_pr hold")
    with write_txn(conn):
        if frozen(conn, task_id):
            raise ValueError("home transfer owns this card; hold release deferred")
        error = precondition(conn)
        if error:
            raise ValueError(error)
        task = kb.get_task(conn, task_id)
        if (task is None or task.status != "ready" or task.current_run_id is not None
                or task.claim_lock or task.claim_expires):
            raise ValueError("hold release requires unclaimed READY target")
        if foreign_copy(conn, task_id):
            raise ValueError("hold release requires local home authority")
        if _event_tip(conn, task_id) != tip:
            raise ValueError("hold release admission changed; retry")
        row, captured, proofs = _scope(conn, task_id)
        _check_publications(conn, task_id, captured)
        for event in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='hold_released'", (task_id,)):
            if review._checkpoint_matches_run(kb._json_dict(event[0]).get("park"), row):
                raise ValueError("hold release already used for this park")
        kb._append_event(conn, task_id, "hold_released", {
            "via": "owner-wake-hold-release", "supervisor": supervisor,
            "supervisor_board": supervisor_board, "park": _identity(row),
            "prs": sorted(captured), "reviews": proofs,
            "scope": "one_continuation", "merge_authorized": False,
        })


def released_after(conn, task_id, since, pr_urls):
    """A receipt older than any claim, or tied with publication, grants nothing."""
    from hermes_cli.kanban_core_update_requeue import latest_run_skipping_requeues
    from hermes_cli.kanban_wake_check import foreign_copy
    run = latest_run_skipping_requeues(conn, task_id)
    if run is None:
        return False
    urls = {norm_pr(u) for u in pr_urls}
    for event in conn.execute("SELECT payload, created_at FROM task_events WHERE task_id=? AND kind='hold_released'",
                              (task_id,)):
        data = kb._json_dict(event["payload"])
        at = kb._to_epoch(event["created_at"])
        captured = data.get("prs")
        if (at is not None and at > since and at <= int(time.time())
                and data.get("via") == "owner-wake-hold-release"
                and data.get("scope") == "one_continuation" and data.get("merge_authorized") is False
                and review._checkpoint_matches_run(data.get("park"), run)
                and ("run_uid" in data["park"] or not foreign_copy(conn, task_id))
                and isinstance(captured, list) and all(isinstance(u, str) for u in captured)
                and urls and urls.issubset({norm_pr(u) for u in captured})):
            return True
    return False
