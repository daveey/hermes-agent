"""Read-only spend admission on every executor, separate from home-owned triage. [daveey]"""
from __future__ import annotations


def claim_check(conn, task_id):
    """Prepare outside the write lock; the returned predicate rechecks in the claim transaction.

    The same accounting, per-card overrides and publication-owner exception as ``hold`` apply to
    local and foreign claims. Only home writes triage/notices. A PR observation is obtained outside
    the transaction and may spare only that same recognised publication on the transactional read.
    A newly exhausted budget without a preflight PR observation refuses until the next attempt.
    """
    from hermes_cli import kanban_spend_budget as budget
    from hermes_cli import kanban_spend_decision as decision
    from hermes_cli import kanban_spend_pr_owner as owner

    board = budget.limits()
    pr = None
    if not decision.pending(conn, task_id):
        lim = budget.card_limits(conn, task_id, board)
        if budget.over(budget.spend(conn, task_id), lim) is not None:
            pr = owner.open_core_pr(conn, task_id)

    def refused():
        if decision.pending(conn, task_id):
            return True
        lim = budget.card_limits(conn, task_id, board)
        if budget.over(budget.spend(conn, task_id), lim) is None:
            return False
        return pr is None or pr not in owner.recognised_prs(conn, task_id)

    return refused
