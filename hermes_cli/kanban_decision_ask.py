"""A park that waits on the card's own chat posts a notice that wakes that chat. [daveey]

A worker that needs its owning orchestrator (the chat that created or shepherds the card) to decide
something used to say so in a comment and park on a clock: ``awaiting={kind:'external', ref:'Owning
orchestrator of comments ...'}``. Neither a comment nor a ``scheduled`` event is a notify kind, so a
dormant orchestrator chat was never woken: t_01a1d774f7486668 sat parked for a week on three decisions
its orchestrator never saw (2026-11-18).

``kanban_awaiting`` records such a wait as ``kind='chat'`` (declared, or reclassified from an
``external`` or inferred wait that names the orchestrator) and refuses it when no chat subscribes to
the card. After the park, :func:`post_notice` appends ``decision_requested`` in the same transaction.
That kind is a gateway wake kind and a ``tui`` notify kind, so a live chat gets it in its queue, a
Mac ``notify+wake`` row starts a turn, and the unloaded sweeper wakes a dormant ``tui`` chat.
"""
from __future__ import annotations

from typing import Any

NOTICE_KIND = "decision_requested"


def is_chat_wait(awaiting: Any) -> bool:
    return isinstance(awaiting, dict) and awaiting.get("kind") == "chat"


def notice_text(payload: dict) -> str:
    """Shared gateway/TUI wording: the worker's question, and how its answer reaches the card."""
    ask = str(payload.get("why") or payload.get("reason") or "")
    if len(ask) > 6000:
        ask = ask[:4500] + "\n[... full text in the card comments ...]\n" + ask[-1500:]
    return ("This card is parked until you decide:\n" + ask
            + "\nAnswer with a comment on the card (kanban_comment); the comment wakes it. "
            "If you cannot decide, block it with kind='needs_input' and a human_request for David.")


def post_notice(conn, task_id: str, *, reason: str | None, awaiting: Any, run_id: int | None = None) -> None:
    """Called only after a successful park, inside its write transaction; no external delivery here."""
    if not is_chat_wait(awaiting):
        return
    from hermes_cli import kanban_db as kb
    payload = {"reason": reason, "why": awaiting.get("why"), "ref": awaiting.get("ref")}
    kb._append_event(conn, task_id, NOTICE_KIND, {k: v for k, v in payload.items() if v}, run_id=run_id)
