"""Text of the live card note steered into a running worker (split from ``kanban_tools``). [daveey]"""
from __future__ import annotations

import time
from typing import Any


def _comment_stamp(c: Any, own_identity: str, agent: bool = False) -> str:
    """``default (another session) at 04:50 PDT``: who wrote the note and when, so a worker
    can tell an operator note from its own even when both run as the same profile. A note an
    agent wrote (``writer_kind='agent'``) says so: ``default (agent chat, another session)``. [daveey]"""
    author = (c.author or "").strip() or "operator"
    tags = (["agent chat"] if agent else []) + (["another session"] if author == own_identity else [])
    who = f"{author} ({', '.join(tags)})" if tags else author
    try:
        when = time.strftime("%H:%M %Z", time.localtime(int(c.created_at)))
    except (TypeError, ValueError, OverflowError):
        return who
    return f"{who} at {when}"


def live_note(fresh: list, own_identity: str, agent_ids: set) -> str:
    """The OUT-OF-BAND note for ``fresh`` comments. [daveey] Say when only agents wrote it: readers
    (ask-gate) must not take a chat agent's "check it yourself" for David's words (agent-261aa31f49)."""
    lines = [f"- {_comment_stamp(c, own_identity, c.id in agent_ids)}: {c.body.strip()}" for c in fresh]
    source = "another agent session" if all(c.id in agent_ids for c in fresh) else "the operator"
    return ("New note" + ("s" if len(fresh) > 1 else "")
            + f" on your kanban task from {source} (delivered mid-run). "
            + "Take it into account for the work you're doing right now:\n" + "\n".join(lines))
