"""Strip a kanban worker's / delegate child's identity env from top-level launches. [daveey]

A dispatcher-spawned worker carries ``HERMES_KANBAN_TASK``/``..._WORKSPACE``/``..._DB`` pins and,
in delegate children, ``HERMES_DELEGATED_CHILD_CONTEXT``. When a worker ran ``hermes update`` the
update reopened Hermes Desktop with ``/usr/bin/open``, which hands the caller's environment to the
app: the Desktop (and every backend and terminal it spawned) then believed it was that worker's
delegate child, so every chat's kanban tools refused ("delegate_task child contexts cannot mutate
Kanban") and the board was pinned to the worker's DB (2026-10-21, Hermes.app pid 59331).

A Desktop, ``serve``/``dashboard`` backend or messaging gateway is never a worker. Each strips
these keys at launch, and code that reopens Desktop passes :func:`clean_launch_env`.
"""
from __future__ import annotations

import os
from typing import MutableMapping, Optional, Sequence

# The key family and the marker rule live in agent/delegation_context.py so they can't drift.
from agent.delegation_context import (
    AGENT_SCOPE_MARKER_KEYS as WORKER_MARKER_KEYS,
    strip_agent_scope_env,
)

# First positional argv of a top-level surface (never a worker).
TOP_LEVEL_COMMANDS = frozenset({"serve", "dashboard", "gui", "desktop", "gateway"})


def scrub_leaked_worker_env(environ: Optional[MutableMapping[str, str]] = None) -> list[str]:
    """Remove the agent-scope env family (worker/delegate/session/cron/RPC identity) from
    ``environ`` (default ``os.environ``) when any worker marker is present. Returns the removed
    key names (empty when nothing leaked)."""
    return strip_agent_scope_env(os.environ if environ is None else environ)


def clean_launch_env(base: Optional[MutableMapping[str, str]] = None) -> dict[str, str]:
    """A copy of ``base`` (default ``os.environ``) safe to hand to a top-level app launch."""
    env = dict(os.environ if base is None else base)
    scrub_leaked_worker_env(env)
    return env


def _first_positional(argv: Sequence[str]) -> str:
    return next((arg for arg in argv if arg and not arg.startswith("-")), "")


def scrub_for_top_level_launch(argv: Sequence[str]) -> list[str]:
    """Scrub ``os.environ`` when ``argv`` (``sys.argv[1:]``) launches a top-level surface."""
    if _first_positional(argv) not in TOP_LEVEL_COMMANDS:
        return []
    removed = scrub_leaked_worker_env()
    if removed:
        import logging

        logging.getLogger(__name__).warning(
            "hermes %s: dropped inherited agent-scope env %s (launched from a kanban worker/delegate child)",
            _first_positional(argv), ", ".join(removed))
    return removed
