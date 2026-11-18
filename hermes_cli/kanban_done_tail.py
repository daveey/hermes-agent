"""Publish done-announcement tails without treating unfinished acceptance as done. [daveey]"""
from __future__ import annotations

import re
from typing import Any

NOTICE_KIND = "park_progress_notice"
_DONE = re.compile(
    r"\beverything else is done\b|\bonly (?:step|thing|item) left\b|\bonly remaining\b|"
    r"\b(?:is now|was|has been|already) (?:shipped|merged)\b|"
    r"(?:^|[.;\n])\s*(?:shipped|merged)\b", re.IGNORECASE,
)


def needs_notice(reason: str | None, *, watch: bool = False, awaiting: Any = None) -> bool:
    """Watches already free the chat; a validated card producer explicitly owns unfinished work.

    A bare 'merged' in 'wait until merged' is not a done announcement. 'Only remaining' can name
    required acceptance, not just cleanup: publish progress rather than refuse or auto-complete it.
    """
    return bool(not watch and not (isinstance(awaiting, dict) and awaiting.get("kind") == "card")
                and _DONE.search(reason or ""))


def notice_text(payload: dict) -> str:
    """Shared gateway/TUI wording: preserve the worker's report without certifying completion."""
    reason = str(payload.get("reason") or "")
    if len(reason) > 8000:
        reason = reason[:6000] + "\n[… full report in the card comment …]\n" + reason[-2000:]
    return ("Progress report while parked (not a completion):\n" + reason
            + "\nThe card remains scheduled for its pending check. If the deliverable is finished and only "
            "a follow-up check remains, use kanban_schedule(watch=true), or kanban_complete plus a child "
            "watch card. Do not mark unfinished acceptance complete.")


def post_notice(conn, task_id: str, *, reason: str | None, watch: bool = False,
                awaiting: Any = None, run_id: int | None = None) -> None:
    """Called only after a successful park, inside its write transaction; no external delivery here."""
    if not needs_notice(reason, watch=watch, awaiting=awaiting):
        return
    from hermes_cli import kanban_db as kb
    payload = {"reason": reason}
    kb.add_comment(conn, task_id, "kanban-park", "Progress report while parked (not a completion):\n" + str(reason),
                   system=True)
    kb._append_event(conn, task_id, NOTICE_KIND, payload, run_id=run_id)
