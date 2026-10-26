"""Read-only terminal-PR wake probe; waking is never delivery acceptance.

Run under the task's Hermes environment with an exact GitHub PR URL. Uses the
same profile-scoped transport/broker configuration as PR acceptance. [daveey]
"""
from __future__ import annotations

import argparse
import re
import subprocess
from urllib.parse import quote


def _supersession(api, repo: str, sha: str, job: str, home: str) -> tuple[int, str]:
    """Supplied evidence can wake reconciliation, never certify required CI."""
    evidence = api(f"repos/{repo}/actions/jobs/{job}", profile_home=home)
    if (not isinstance(evidence, dict)
            or not isinstance(evidence.get("head_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", evidence["head_sha"])
            or evidence.get("status") not in {"queued", "in_progress", "completed", "waiting", "pending", "requested"}
            or (evidence["status"] == "completed" and not isinstance(evidence.get("conclusion"), str))):
        raise ValueError("incomplete job evidence")
    if (evidence["head_sha"] != sha or evidence.get("status") != "completed"
            or evidence.get("conclusion") != "success"):
        return 1, "PR probe: pending; supplied supersession job has not verified this commit"
    repository = api(f"repos/{repo}", profile_home=home)
    branch = repository.get("default_branch") if isinstance(repository, dict) else None
    if not isinstance(branch, str) or not branch:
        raise ValueError("default branch unavailable")
    comparison = api(f"repos/{repo}/compare/{sha}...{quote(branch, safe='')}", profile_home=home)
    state = comparison.get("status") if isinstance(comparison, dict) else None
    if state in {"ahead", "identical"}:
        return 0, ("PR probe: reconcile verified containing-default-branch commit and successful job; "
                   "not acceptance. Confirm replacement ownership and all required exact-head CI "
                   "before declaring delivery; the original PR is still OPEN.")
    if state in {"behind", "diverged"}:
        return 1, "PR probe: pending; superseding commit is not contained in default branch"
    raise ValueError("comparison unavailable")


def inspect_pr(url: str, superseding_commit: str | None = None,
               verification_job: str | None = None) -> tuple[int, str]:
    """Return the existing wake contract: inspect now=0, pending=1, error=2."""
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_pr_acceptance as acceptance

    match = acceptance._PR.fullmatch(url)
    if not match:
        return 2, "PR probe: invalid exact GitHub PR URL"
    if (superseding_commit is not None or verification_job is not None) and not (
            isinstance(superseding_commit, str) and re.fullmatch(r"[0-9a-f]{40}", superseding_commit)
            and isinstance(verification_job, str) and re.fullmatch(r"[1-9][0-9]*", verification_job)):
        return 2, "PR probe: supply both exact superseding commit and positive verification job ID"
    try:
        data = acceptance._api(f"repos/{match[1]}/pulls/{match[2]}",
                               profile_home=str(get_hermes_home()))
        if (not isinstance(data, dict) or data.get("html_url") != url
                or type(data.get("merged")) is not bool):
            raise ValueError("incomplete PR evidence")
        state, merged = data.get("state"), data["merged"]
        if state == "open" and not merged:
            if superseding_commit is not None:
                return _supersession(acceptance._api, match[1], superseding_commit,
                                     verification_job, str(get_hermes_home()))
            return 1, "PR probe: pending (OPEN)"
        if state == "closed" and merged:
            return 0, ("PR probe: merged; wake for integration and required CI verification, "
                       "not acceptance or permission to deploy.")
        if state == "closed" and not merged:
            return 0, ("PR probe: reconcile CLOSED/unmerged wait; not acceptance. "
                       "Distinguish abandonment from verified supersession using containing-main "
                       "commit and exact successful CI evidence. Preserve all delivery gates.")
        raise ValueError("unknown PR state")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError,
            acceptance._GateAuthError, acceptance._GateSetupError):
        # No provider bodies, command args, tokens, or raw exception text in events.
        return 2, "PR probe: evidence unavailable; inspect profile transport/config, not a pending result"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr_url", help="Exact https://github.com/OWNER/REPO/pull/NUMBER URL")
    parser.add_argument("--superseding-commit", help="Known replacement commit; requires --verification-job")
    parser.add_argument("--verification-job", help="Successful Actions job for that exact commit")
    args = parser.parse_args(argv)
    rc, message = inspect_pr(args.pr_url, args.superseding_commit, args.verification_job)
    print(message)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
