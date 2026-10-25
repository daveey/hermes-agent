"""Read-only terminal-PR wake probe; waking is never delivery acceptance.

Run under the task's Hermes environment with an exact GitHub PR URL. Uses the
same profile-scoped transport/broker configuration as PR acceptance. [daveey]
"""
from __future__ import annotations

import argparse
import subprocess


def inspect_pr(url: str) -> tuple[int, str]:
    """Return the existing wake contract: inspect now=0, pending=1, error=2."""
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_pr_acceptance as acceptance

    match = acceptance._PR.fullmatch(url)
    if not match:
        return 2, "PR probe: invalid exact GitHub PR URL"
    try:
        data = acceptance._api(f"repos/{match[1]}/pulls/{match[2]}",
                               profile_home=str(get_hermes_home()))
        if (not isinstance(data, dict) or data.get("html_url") != url
                or type(data.get("merged")) is not bool):
            raise ValueError("incomplete PR evidence")
        state, merged = data.get("state"), data["merged"]
        if state == "open" and not merged:
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
    args = parser.parse_args(argv)
    rc, message = inspect_pr(args.pr_url)
    print(message)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
