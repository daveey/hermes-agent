"""Resume idle PR publishers after bounded-age, exact-head green CI observations.

Admission is to finish the existing PR, never permission to merge. Repeated crash
recovery remains subject to ordinary run/spend breakers; green CI is not review.
"""
from __future__ import annotations

from hermes_cli.kanban_db_dispatch_pr_policy import norm_pr
from hermes_cli import kanban_pr_idle_checks as checks


def clear_cache() -> None:
    checks.clear_cache()


def _idle(conn, task_id: str) -> bool:
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    return bool(row and row[0] == "ready" and not conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL LIMIT 1", (task_id,),
    ).fetchone() and not conn.execute(
        "SELECT 1 FROM task_wake_checks WHERE task_id=?", (task_id,),
    ).fetchone())


def _identity(conn, task_id):
    row = conn.execute("SELECT completion_contract, assignee FROM tasks WHERE id=?", (task_id,)).fetchone()
    comments = conn.execute("SELECT MAX(id) FROM task_comments WHERE task_id=?", (task_id,)).fetchone()[0]
    run = conn.execute("SELECT MAX(id) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
    return (*row, comments, run) if row else None


def releases(conn, task_id: str, states: dict[str, str | None]) -> bool:
    """Every unfinished provenance-filtered holding PR must have green exact-head CI."""
    from hermes_cli import kanban_db_dispatch_pr_state as prs
    from hermes_cli.kanban_pr_acceptance import validate_contract

    if not _idle(conn, task_id):
        return False
    before = _identity(conn, task_id)
    if before is None:
        return False
    contract, assignee = before[:2]
    try:
        contract = validate_contract(contract)
    except ValueError:
        return False
    unfinished = {url: state for url, state in states.items() if state not in prs.TERMINAL_STATES}
    if not unfinished or any(state != "OPEN" for state in unfinished.values()):
        return False
    if contract.startswith("https://github.com/") and norm_pr(contract) not in {norm_pr(u) for u in states}:
        return False
    database = tuple(tuple(r) for r in conn.execute("PRAGMA database_list"))
    results = []
    for url in unfinished:
        head = prs.pr_head(url)
        if head is None:
            return False
        key = (database, task_id, before, url, head)
        results.append(checks.green(key, url, assignee, head))
    # A newly published PR/run cannot reuse a prior observation, even on another board.
    return bool(all(results) and before == _identity(conn, task_id) and _idle(conn, task_id))
