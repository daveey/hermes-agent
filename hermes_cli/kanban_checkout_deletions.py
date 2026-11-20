"""Narrow checkout-deletion exception for card placement, never for tree removal. [daveey]"""
from __future__ import annotations

import subprocess


def published_checkout_deletions(tree: str, budget) -> bool:
    """Only missing checkout files, with HEAD contained in its branch's upstream.

    Permit uniform unstaged deletions, or uniform staged deletions with an entirely
    empty index (the owning-clone shape observed on a pool worker). Partial staged
    deletions, hidden edits and every unknown/error retain the existing pin.
    """
    from hermes_cli._subprocess_compat import noninteractive_repo_git_env
    from hermes_cli.git_budget import timed_out, timeout_for
    from hermes_cli.worktree_archive import ArchiveError
    from hermes_cli.worktree_ops import _git
    from hermes_cli.worktree_retention import hidden_index_edits

    try:
        env = noninteractive_repo_git_env(tree, budget=budget)
        if env is None:
            return False

        def probe(args):
            result = _git(args, tree, timeout=timeout_for(budget, 10), stdin=subprocess.DEVNULL, env=env)
            if result.returncode != 0 or result.stderr.strip():
                return None
            return result.stdout

        status = probe(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        if status is None:
            return False
        entries = [entry for entry in status.split("\0") if entry]
        if not entries:
            return False
        unstaged = all(entry.startswith(" D ") for entry in entries)
        staged = all(entry.startswith("D  ") for entry in entries)
        if not unstaged and not (staged and probe(["ls-files", "-z"]) == ""):
            return False
        branch = probe(["symbolic-ref", "-q", "HEAD"])
        if not branch or not branch.strip().startswith("refs/heads/"):
            return False
        if probe(["merge-base", "--is-ancestor", "HEAD", "@{upstream}"]) is None:
            return False
        # A local upstream/trunk is not publication. Require positive remote-ref
        # reachability here; the legacy unpushed predicate allows a local-trunk
        # baseline when there are no remote refs, which must not grant this exception.
        published = probe(["for-each-ref", "--contains=HEAD", "--format=%(refname)", "refs/remotes/"])
        if not published or not published.strip():
            return False
        return not hidden_index_edits(tree, env=env, budget=budget)
    except subprocess.TimeoutExpired:
        raise timed_out(budget) from None
    except (ArchiveError, OSError, subprocess.SubprocessError, ValueError):
        return False
