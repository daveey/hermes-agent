"""Ship review contract: refuse a review card's completion that ``hermes-core-wt ship`` cannot read. [daveey]

``hermes-core-wt ship --review-task <card>`` lands a core PR only when the review card's completion metadata
carries ``verdict == "approve"``, a 40-hex ``reviewed_sha`` equal to the PR head and ``reviewed_pr``. Review
workers completed with ``review_verdict``, ``review_outcome`` or ``reviewed_head`` instead; the card showed done
and approved, ship refused it later and a whole new review card was filed for the same head.

This module catches that gap at completion time, while the reviewer can still fix it in the same run:

- A card *states the contract* when its body names a pull request of the core fork, ``reviewed_pr`` and the
  head key (``reviewed_sha``, or the ``reviewed_head`` some briefs misnamed it). Ship-filed and re-issued review
  cards do; so do hand-filed ones whose brief misnamed a key (``review_verdict``, ``reviewed_head``), which is
  exactly the case to fix. A verdict word is not required: one reviewer-lane brief asked for none and the reviewer
  wrote ``review_outcome``.
- On such a card, a completion whose metadata looks like a review result (any review key) must carry exactly
  ``verdict`` in {approve, request_changes}, a lowercase 40-hex ``reviewed_sha`` and ``reviewed_pr`` naming the
  pull. A landing/implementation handoff (``published_pr``) and a completion with no review key at all (a card
  that only discusses the contract) are not review results and are left alone.

Ship itself is unchanged: it accepts no aliases and infers no sha.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from hermes_cli.kanban_db_dispatch_pr_policy import BUILTIN_GITHUB_REPO_ALIASES

REJECTION = "review_contract"
VERDICTS = ("approve", "request_changes")
CORE_REPOS = frozenset(r.lower() for pair in BUILTIN_GITHUB_REPO_ALIASES.items() for r in pair)
_SHA40 = re.compile(r"[0-9a-f]{40}")
_PULL = re.compile(r"github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/\d+", re.IGNORECASE)
_PR_REF = re.compile(r"(?:https://github\.com/[\w.-]+/[\w.-]+/pull/\d+|[\w.-]+/[\w.-]+#\d+)")
# Keys a reviewer uses for its verdict or the reviewed head: their presence makes the completion a review result.
_REVIEW_KEYS = ("verdict", "review_verdict", "review_outcome", "reviewed_sha", "reviewed_head", "reviewed_pr",
                "approved", "approval")


def states_contract(body: Optional[str]) -> bool:
    """True when ``body`` asks for the ship review metadata on a core-fork pull request."""
    text = body or ""
    if "reviewed_pr" not in text:
        return False
    if "reviewed_sha" not in text and "reviewed_head" not in text:
        return False
    return any(m.group("repo").lower() in CORE_REPOS for m in _PULL.finditer(text))


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
    """The refusal text for a contract card's unreadable review completion, else None."""
    if not states_contract(body) or not is_review_result(metadata):
        return None
    found = problems(metadata)
    if not found:
        return None
    return ("kanban_complete refused: this card states the ship review contract, and `hermes-core-wt ship "
            "--review-task` reads only metadata.verdict, metadata.reviewed_sha and metadata.reviewed_pr. "
            + "; ".join(found) + ". Nothing changed: retry kanban_complete with the same summary and metadata "
            "that also carries {\"verdict\": \"approve\" | \"request_changes\", \"reviewed_sha\": \"<40-hex head>\", "
            "\"reviewed_pr\": \"https://github.com/<owner>/<repo>/pull/<n>\"}.")
