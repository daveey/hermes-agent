"""``hermes update --up-to <sha>``: fast-forward main only as far as one first-parent commit.

hermes-sync applies a verified Desktop prebuild of an older ``main`` commit C (installed < C <
origin/main) instead of waiting for a build of the tip. Core must then stop at C: everything
that names the update target (preflight, commit point, interrupted-pull marker, receipt and
fleet restart marker) uses C, the move is ``merge --ff-only C`` on branch main, and the fork's
upstream sync and push are skipped (never merge upstream onto a non-tip commit).

Every check here runs before any tree write or commit point; a refusal exits 1 with the
install unchanged. A later plain ``hermes update`` from C moves to the tip as usual.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable, NoReturn

from hermes_cli.update_cmd_common import _record_stop

FLAG = "--up-to"
UP_TO_ACCEPTS_HEAD = True  # Text capability marker read by hermes-sync in the live core.

#: (argparse dest, flag): flags that pick another target or do not apply an update.
_INCOMPATIBLE = (
    ("branch", "--branch"), ("channel", "--channel"), ("set_channel", "--set-channel"),
    ("check", "--check"), ("plan", "--plan"), ("install_id", "--install-id"),
    ("list_venv_holders", "--list-venv-holders"),
)
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def requested(args) -> str | None:
    """The ``--up-to`` SHA this run was given, else None."""
    return getattr(args, "up_to", None) or None


def refuse(sha: str, reason: str, *, without_receipt: str | None = None) -> NoReturn:
    """Print the one refusal line, record ``up_to_refused`` and exit 1."""
    print(f"✗ {FLAG} {sha[:10]}: {reason}. No update was applied.")
    _record_stop("up_to_refused", without_receipt=without_receipt)
    sys.exit(1)


def refuse_unsupported_invocation(args) -> None:
    """Before any update side effect: a malformed SHA, or a flag ``--up-to`` cannot combine with."""
    sha = requested(args)
    if sha is None:
        return
    if not _FULL_SHA.fullmatch(sha):
        refuse(sha, "expected a full 40-hex commit sha", without_receipt="refused")
    for dest, flag in _INCOMPATIBLE:
        if getattr(args, dest, None):
            refuse(sha, f"cannot be combined with {flag}", without_receipt="refused")


def refuse_unsupported_install(sha: str | None, *, use_zip: bool, release_sha: str | None, branch: str,
                               stop: Callable[[], None]) -> None:
    """Only a git source install updating branch main through the default path can stop early."""
    if sha is None:
        return
    reason = ("this install updates from a ZIP archive, not a git checkout" if use_zip
              else "the update channel selects a release, not branch main" if release_sha
              else f"the update tracks branch '{branch}', not main" if branch != "main" else None)
    if reason is not None:
        stop()
        refuse(sha, reason)


def _out(git, args: list[str]) -> str:
    result = git(args)
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def select_target(git_cmd, root: Path, sha: str | None, current_branch: str, target_ref: str, request: dict,
                  *, stop: Callable[[], None]) -> str:
    """The update target: *target_ref* unchanged without ``--up-to``; else *sha*, validated against
    the fetched ``origin/main`` and pinned as the run's ``expected_sha`` (receipt, completion).

    Requires the checkout on branch main, *sha* a commit, HEAD an ancestor of it (or equal),
    and *sha* on origin/main's first-parent line. Exits 1 (after ``stop()``) otherwise.
    """
    if sha is None:
        return target_ref
    from hermes_cli.update_cmd_check import _git

    def git(args):
        return _git(git_cmd, root, args)

    def fail(reason: str) -> NoReturn:
        stop()
        refuse(sha, reason)

    if current_branch != "main":
        fail("the checkout is on a detached HEAD, not branch main" if current_branch == "HEAD"
             else f"the checkout is on branch '{current_branch}', not branch main")
    if _out(git, ["rev-parse", "-q", "--verify", f"{sha}^{{commit}}"]) != sha:
        fail("not a commit in this checkout")
    tip = _out(git, ["rev-parse", "-q", "--verify", "refs/remotes/origin/main^{commit}"])
    head = _out(git, ["rev-parse", "-q", "--verify", "HEAD^{commit}"])
    if not tip or not head:
        fail("origin/main or HEAD does not resolve")
    if git(["merge-base", "--is-ancestor", head, sha]).returncode != 0:
        fail(f"HEAD {head[:10]} is not an ancestor of it")
    # An equal target still needs first-parent membership: being an ancestor via
    # a merged side branch is not enough. Include HEAD only for the zero-move case.
    line = tip if sha == head else f"{head}..{tip}"
    if sha not in _out(git, ["rev-list", "--first-parent", line]).split():
        fail(f"not on origin/main's first-parent line after HEAD (origin/main is {tip[:10]})")
    count = _out(git, ["rev-list", "--count", f"{head}..{sha}"]) or "?"
    print(f"→ Updating up to {sha[:10]} (origin/main is {tip[:10]}; {count} commit(s))")
    request["expected_sha"] = sha
    return sha
