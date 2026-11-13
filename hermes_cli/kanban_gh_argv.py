"""The ``gh`` argv a dispatcher-side GitHub read runs. [daveey]

A gateway or dispatcher service has a minimal PATH and often no ambient ``gh`` login (the hub: "You are not
logged into any GitHub hosts", no GH_TOKEN). Its GitHub reads go through the home's
``kanban.pr_acceptance.gh_command_prefix`` (a credential-broker wrapper such as
``token-broker exec --scope user/github --``) and a ``gh`` resolved from ~/bin / ~/.local/bin, as the
PR-acceptance gate does. A read that skips the prefix fails on the hub with gh's auth exit (4): the CI-run progress
check did, so it answered ``unknown`` for every run and never refused a park or woke a card (agent-c2b8fc1abf).
"""
from __future__ import annotations


class GhArgvUnavailable(RuntimeError):
    """No ``gh`` argv can be built. The message is fixed text: config errors may carry credentials."""


def dispatcher_gh_argv() -> list[str]:
    """``[*gh_command_prefix, gh]`` for this home. Raises :class:`GhArgvUnavailable` with no config text."""
    from hermes_cli import kanban_pr_acceptance as kpa
    from hermes_platform.resolver import github as ghres

    resolved = ghres.resolve_gh()
    if not resolved.found:
        raise GhArgvUnavailable("gh not found on PATH, ~/bin or ~/.local/bin")
    try:
        prefix = kpa._gh_command_prefix(None)
    except (RuntimeError, OSError, ValueError, TypeError, ImportError):
        raise GhArgvUnavailable("kanban.pr_acceptance.gh_command_prefix is unreadable or invalid") from None
    return [*prefix, *resolved.command]
