"""One time budget shared by every git call of a scan. [daveey]

A scan that runs many git calls (the move guard's :func:`hermes_cli.kanban_move_pool.tree_state` over a card's
trees) bounds its total time by handing each helper a :data:`GitBudget`: called with a call's own timeout cap, it
returns the timeout that call may use (the cap, or less when the deadline is nearer) and raises
:class:`GitUnchecked` once the deadline has passed. Sequential calls that each take at most the time left end by
the deadline, whatever their number.

With a budget, a helper reports a check it could not finish (time spent, git timed out, a hardening probe that
gave no usable answer) by raising :class:`GitUnchecked` instead of turning it into a verdict ("dirty", "unpushed"): the
caller then says the tree is unchecked, which it still treats as state (fail safe). Without one, helpers keep
their own fail-safe verdicts.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

GitBudget = Callable[[float], float]

BUDGET_SPENT = "git budget spent"
GIT_TIMED_OUT = "git timed out"
# noninteractive_repo_git_env returned None: its probe gave no answer, or an answer it refuses (non-file include
# origin, FIFO/directory include target, too many include targets or filter keys). No hardened env, no verdict.
NO_FILTER_ENV = "git filter discovery gave no usable answer"


class GitUnchecked(Exception):
    """A budgeted git check reached no verdict. Not a ``ValueError``/``OSError``/``SubprocessError``: the fail-soft
    handlers that turn those into a verdict (False / "unpushed" / "dirty") must let it through."""


def deadline_budget(seconds: float, clock: Callable[[], float] = time.monotonic) -> GitBudget:
    """A budget that ends ``seconds`` after now on ``clock``."""
    deadline = clock() + seconds

    def timeout(cap: float) -> float:
        left = deadline - clock()
        if left <= 0:
            raise GitUnchecked(BUDGET_SPENT)
        return min(cap, left)
    return timeout


def timeout_for(budget: Optional[GitBudget], cap: float) -> float:
    """``cap`` drawn from ``budget`` (raises :class:`GitUnchecked` once it is spent), or ``cap`` with no budget."""
    return cap if budget is None else budget(cap)


def ensure_left(budget: Optional[GitBudget]) -> None:
    """Raise :class:`GitUnchecked` when ``budget`` is spent: a git timeout the deadline cut short is no verdict."""
    timeout_for(budget, 0.0)


def timed_out(budget: Optional[GitBudget]) -> GitUnchecked:
    """The :class:`GitUnchecked` for a git timeout under ``budget``: the budget's end, or git's own slowness."""
    try:
        ensure_left(budget)
    except GitUnchecked as spent:
        return spent
    return GitUnchecked(GIT_TIMED_OUT)
