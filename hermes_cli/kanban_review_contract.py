"""Ship review contract: refuse a review card's completion that ``hermes-core-wt ship`` cannot read. [daveey]

``hermes-core-wt ship --review-task <card>`` lands a core PR only when the review card's completion metadata
carries ``verdict == "approve"``, a 40-hex ``reviewed_sha`` equal to the PR head and ``reviewed_pr``. Review
workers completed with ``review_verdict``, ``review_outcome`` or ``reviewed_head`` instead; the card showed done
and approved, ship refused it later and a whole new review card was filed for the same head.

This module catches that gap at completion time, while the reviewer can still fix it in the same run:

- The gate applies to a card whose body names a pull request of the core fork (``names_core_pull``). It does not
  require the body to spell out ``reviewed_pr`` or ``reviewed_sha``: a hand-written brief that asked for the wrong
  key (``pr`` for the URL, PR #1462's land-lane review) turned the earlier wording rule off exactly when the
  reviewer was about to write a completion ship cannot read. When the body does not spell the contract out
  (``states_contract``), the completion must also carry a verdict key: landing and cleanup handoffs on core-pull
  cards record ``reviewed_sha``/``reviewed_pr`` without one (replayed over the real boards) and are not reviews.
- On such a card, a completion whose metadata looks like a review result (any review key) must carry exactly
  ``verdict`` in {approve, request_changes}, a lowercase 40-hex ``reviewed_sha`` and ``reviewed_pr`` naming the
  pull. A landing/implementation handoff (``published_pr``) and a completion with no review key at all are
  normally left alone. Ship's explicit "Independent exact-head review of" brief is the exception: its stated
  metadata contract is required even when every review key is absent, so the reviewer can retry in the same run.

Ship itself is unchanged: it accepts no aliases and infers no sha.

``independence_refusal`` closes the other half (defect agent-9eea612942): ship also accepts only a review completed
by a dispatched reviewer run with its own session. A landing chat that completed the review card itself (with a
subagent's verdict, from the CLI, or from the dashboard) left a record ship refuses hours later at land time
("its latest run did not complete; no completed review record"). Such a completion is now refused when it is
recorded: on a core-pull card, a verdict-bearing review result from a caller that is not the card's own dispatched
worker run. A card whose body names no core-fork pull still counts as one when the completion's own
``reviewed_pr`` names a core-fork pull (``reviewed_core_pull``, defect agent-8a7de4b89f: a card that named the
PR only in prose escaped the check). No override: ship refuses the record regardless, so an override would only
bring the defect back.
The card's own worker calling the CLI from its terminal shell (the fallback when a kanban tool hits a DB error)
is not outside: ``kanban_worker_shell.run_for`` proves its claimed run, whose completion ship accepts.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from hermes_cli.kanban_db_dispatch_pr_policy import BUILTIN_GITHUB_REPO_ALIASES

REJECTION = "review_contract"
INDEPENDENCE_REJECTION = "review_independence"
VERDICTS = ("approve", "request_changes")
CORE_REPOS = frozenset(r.lower() for pair in BUILTIN_GITHUB_REPO_ALIASES.items() for r in pair)
_SHA40 = re.compile(r"[0-9a-f]{40}")
_PULL = re.compile(r"github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/\d+", re.IGNORECASE)
_PR_REF = re.compile(r"(?:https://github\.com/[\w.-]+/[\w.-]+/pull/\d+|[\w.-]+/[\w.-]+#\d+)")
# Keys a reviewer uses for its verdict or the reviewed head: their presence makes the completion a review result.
_REVIEW_KEYS = ("verdict", "review_verdict", "review_outcome", "reviewed_sha", "reviewed_head", "reviewed_pr",
                "approved", "approval")
# The subset that carries a verdict. On a core-pull card whose body does not spell the contract out, only these
# make a completion a review: landing and cleanup handoffs record `reviewed_sha`/`reviewed_pr` without a verdict.
_VERDICT_KEYS = ("verdict", "review_verdict", "review_outcome", "approved", "approval")


def states_contract(body: Optional[str]) -> bool:
    """True when ``body`` asks for the ship review metadata on a core-fork pull request."""
    text = body or ""
    if "reviewed_pr" not in text:
        return False
    if "reviewed_sha" not in text and "reviewed_head" not in text:
        return False
    return any(m.group("repo").lower() in CORE_REPOS for m in _PULL.finditer(text))


def core_pull(body: Optional[str]) -> Optional[str]:
    """The first core-fork pull request URL ``body`` names, else None."""
    for m in _PULL.finditer(body or ""):
        if m.group("repo").lower() in CORE_REPOS:
            return "https://" + m.group(0)
    return None


def names_core_pull(body: Optional[str]) -> bool:
    """True when ``body`` names a pull request of the core fork: the cards whose review ship lands from."""
    return core_pull(body) is not None


def is_review_result(metadata: Any) -> bool:
    """True when the completion metadata reports a review (a review key) and is not a landing handoff."""
    if not isinstance(metadata, dict) or metadata.get("published_pr"):
        return False
    return any(key in metadata for key in _REVIEW_KEYS)


def problems(metadata: Any) -> list[str]:
    """What ship could not read in ``metadata``; empty when the three keys are exact."""
    md = metadata if isinstance(metadata, dict) else {}
    out = []
    verdict = md.get("verdict")
    if verdict not in VERDICTS:
        named = [k for k in ("review_verdict", "review_outcome", "approved", "approval") if k in md]
        hint = f" ({', '.join(named)} is not read)" if named else ""
        out.append(f"verdict: metadata.verdict is {verdict!r}; it must be exactly \"approve\" or "
                   f"\"request_changes\"{hint}")
    sha = md.get("reviewed_sha")
    if not (isinstance(sha, str) and _SHA40.fullmatch(sha)):
        hint = " (reviewed_head is not read)" if "reviewed_head" in md else ""
        out.append(f"reviewed_sha: metadata.reviewed_sha is {sha!r}; it must be the full 40-hex lowercase head "
                   f"you reviewed{hint}")
    pr = md.get("reviewed_pr")
    if not (isinstance(pr, str) and _PR_REF.fullmatch(pr)):  # exact: ship compares the string as it is
        out.append(f"reviewed_pr: metadata.reviewed_pr is {pr!r}; it must name the pull, "
                   "https://github.com/<owner>/<repo>/pull/<n>")
    return out


def refusal(body: Optional[str], metadata: Any) -> Optional[str]:
    """The refusal text for a review result on a core-fork pull card that ship cannot read, else None."""
    # Ship's generated brief is a review assignment, even when a reviewer forgets every
    # metadata key. Other cards may merely discuss the contract or hand off a landing.
    landing = isinstance(metadata, dict) and metadata.get("published_pr")
    required = (not landing and states_contract(body)
                and re.match(r"\s*Independent exact-head review of\b", body or "", re.IGNORECASE))
    if not required and not is_review_result(metadata):
        return None
    if states_contract(body):
        cause = "this card states the ship review contract"
    else:
        # The body does not spell the contract out: a core-fork pull card still gates a review that carries a
        # verdict (PR1462's brief asked for `pr`); a verdictless landing/cleanup handoff is not a review.
        pull = core_pull(body)
        if pull is None or not any(key in metadata for key in _VERDICT_KEYS):
            return None
        cause = f"this card reviews a core-fork pull ({pull})"
    found = problems(metadata)
    if not found:
        return None
    return (f"kanban_complete refused: {cause}, and `hermes-core-wt ship "
            "--review-task` reads only metadata.verdict, metadata.reviewed_sha and metadata.reviewed_pr. "
            + "; ".join(found) + ". Nothing changed: retry kanban_complete with the same summary and metadata "
            "that also carries {\"verdict\": \"approve\" | \"request_changes\", \"reviewed_sha\": \"<40-hex head>\", "
            "\"reviewed_pr\": \"https://github.com/<owner>/<repo>/pull/<n>\"}.")


def reviewed_core_pull(metadata: Any) -> Optional[str]:
    """The completion's own ``reviewed_pr`` when it names a core-fork pull, else None.

    Review card t_01a1cd9ac1af23fa (defect agent-8a7de4b89f) named PR #1123 only in prose ("softmax-universe/
    daveey-hermes PR #1123"), so ``core_pull(body)`` is None; the chat's completion still recorded
    ``reviewed_pr`` = the core pull URL with a verdict, and ship refused that record at land time.
    """
    pr = metadata.get("reviewed_pr") if isinstance(metadata, dict) else None
    if not isinstance(pr, str):
        return None
    m = _PULL.search(pr) or re.fullmatch(r"(?P<repo>[\w.-]+/[\w.-]+)#\d+", pr.strip())
    return pr.strip() if m and m.group("repo").lower() in CORE_REPOS else None


def independence_refusal(body: Optional[str], metadata: Any, worker_run_id: Optional[int], *,
                         worker_shell: Optional[str] = None) -> Optional[str]:
    """The refusal text for a ship review verdict recorded outside the card's own dispatched worker run, else None.

    ``worker_run_id`` is the caller's dispatcher run id for this card (None for a chat, an orchestrator, the CLI or
    the dashboard; a worker's terminal shell gets it only once ``kanban_worker_shell.run_for`` proves the run). A ship
    review card is one whose body names a core-fork pull (``states_contract`` implies it); when the body names none,
    the verdict's own ``reviewed_pr`` naming a core-fork pull makes it one (``reviewed_core_pull``). Landing handoffs
    (``published_pr``), completions with no verdict key and other cards are left alone. ``worker_shell`` is why the
    proof failed when the caller's ``HERMES_KANBAN_WORKER_TASK`` names this card: the card's own worker, for which
    "leave it for a worker" is the wrong advice.
    """
    if worker_run_id is not None:
        return None
    if not is_review_result(metadata) or not any(key in metadata for key in _VERDICT_KEYS):
        return None
    pull = core_pull(body)
    if pull is not None:
        card = (f"this is a ship review card for {pull}" if states_contract(body)
                else f"this card names core pull {pull}")
    elif (pull := reviewed_core_pull(metadata)) is not None:
        card = f"this completion reviews core pull {pull}"
    else:
        return None
    if worker_shell:
        return (f"{card}, and this shell names it in HERMES_KANBAN_WORKER_TASK, but {worker_shell}. "
                "`hermes-core-wt ship` accepts only a review completed by the card's own dispatched run with its "
                "admitted session, so the CLI fallback cannot record a review ship accepts from here. Retry "
                "kanban_complete with the tool; if the tool keeps failing, kanban_block the card with the error. "
                "Nothing changed.")
    return (f"{card}; `hermes-core-wt ship` accepts only a review completed by a "
            "dispatched reviewer run with its own session, so a completion recorded from this chat or the CLI would "
            "be refused at land time ('no completed review record'). Leave the card for a worker to claim, or post "
            "your findings as a kanban_comment. Nothing changed.")


class IndependenceRefused(ValueError):
    """``independence_refusal`` raised for the CLI and dashboard completion paths (a ValueError they surface)."""


def completion_refusal(body: Optional[str], metadata: Any, worker_run_id: Optional[int]) -> Optional[tuple[str, str]]:
    """``(message, rejection class)`` for the kanban_complete tool, else None. Independence is checked first: a
    verdict recorded outside the card's worker run is worthless to ship whatever its shape, so fixing the shape from
    the same caller must not be the advice."""
    if outside := independence_refusal(body, metadata, worker_run_id):
        return f"kanban_complete refused: {outside}", INDEPENDENCE_REJECTION
    if unreadable := refusal(body, metadata):
        return unreadable, REJECTION
    return None


def refuse_outside(conn, task_id: str, metadata: Any, worker_run_id: Optional[int], *,
                   worker_shell: Optional[str] = None) -> None:
    """Raise IndependenceRefused when ``independence_refusal`` refuses this completion of ``task_id``."""
    from hermes_cli import kanban_db as kb
    msg = independence_refusal(getattr(kb.get_task(conn, task_id), "body", None), metadata, worker_run_id,
                               worker_shell=worker_shell)
    if msg:
        raise IndependenceRefused(f"{INDEPENDENCE_REJECTION}: {msg}")
