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

The subscriber check runs only where the card is owned (subscriptions are machine-local). When a pool
worker's ask reaches the owner by sync and the owner has no chat that can take it, :func:`owner_fallback`
records agent-owned recovery while preserving the schedule and answer probe; missing delivery is not human authority.
"""
from __future__ import annotations

from typing import Any

NOTICE_KIND = "decision_requested"
# An unanswered chat wait records recovery after this interval. Delivery failure never creates a human ask.
ASK_OVERDUE_S = 2 * 3600
UNANSWERED_KIND = "decision_unanswerable"  # diagnostic only: recovery remains agent-owned


def is_chat_wait(awaiting: Any) -> bool:
    return isinstance(awaiting, dict) and awaiting.get("kind") == "chat"


def notice_text(payload: dict) -> str:
    """Shared gateway/TUI wording: the worker's question, and how its answer reaches the card."""
    ask = str(payload.get("why") or payload.get("reason") or "")
    if len(ask) > 6000:
        ask = ask[:4500] + "\n[... full text in the card comments ...]\n" + ask[-1500:]
    if isinstance(payload.get("actionability"), dict) and payload["actionability"].get("kind") == "administrative_only":
        return ("Administrative reconsideration for the accountable owner (not a request for the user):\n" + ask
                + "\nComments record a decision only; they never release or rerun the root scope. "
                "Use the existing supported authority and admission gates; a notice grants neither.")
    return ("Decision for the creator agent (not a request for the user). This card is parked until you decide:\n" + ask
            + "\nAnswer with a comment on the card (kanban_comment); the comment wakes it. "
            "Retain agent recovery on quota/offline failures. Escalate to the user only for a concrete human-only "
            "action, after try-to-unblock and generate-unblock-card, with a meaningful scope and evidence.")


def post_notice(conn, task_id: str, *, reason: str | None, awaiting: Any, run_id: int | None = None) -> None:
    """Called only after a successful park, inside its write transaction; no external delivery here."""
    if not is_chat_wait(awaiting):
        return
    from hermes_cli import kanban_db as kb
    payload = {"reason": reason, "why": awaiting.get("why"), "ref": awaiting.get("ref")}
    kb._append_event(conn, task_id, NOTICE_KIND, {k: v for k, v in payload.items() if v}, run_id=run_id)


def owner_fallback(conn, task_id: str) -> bool:
    """On the owning gateway, after an imported ``decision_requested``: no wakeable subscriber means nobody gets
    the ask, so record agent recovery (:func:`escalate`). True when recorded."""
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_wake_check as kwc
    if kaw.has_chat_subscriber(conn, task_id):
        return False
    own = kwc.own_gateway(conn)
    return escalate(conn, task_id, f"no chat subscribed to it on {own or 'its owning gateway'} can be woken")


def escalate(conn, task_id: str, why_unanswered: str, *, trust_probe: bool = False) -> bool:
    """Record agent-owned recovery without promoting delivery failure to human authority.

    Keep the original schedule, probe and owner: a timeout, quota failure or absent chat
    proves no human-only action. True when recovery was recorded for this ask. A real
    human escalation must be authored separately with concrete action/scope evidence.
    """
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task is None or task.status != "scheduled":
            return False
        record, _reason, _wake_check, park_at = _latest_chat_park(conn, task_id)
        # A freshly run comment probe is authoritative, including its explicit filters.
        if not trust_probe and record is not None and kaw._answered_since(conn, task_id, park_at):
            return False
        asks = [e for e in kb.list_events(conn, task_id) if e.kind == NOTICE_KIND]
        if not asks:
            return False
        ask = asks[-1]
        # One recovery observation per ask, not one feed write every probe tick.
        if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=? AND id>? LIMIT 1",
                        (task_id, UNANSWERED_KIND, ask.id)).fetchone():
            return False
        payload = ask.payload or {}
        question = str(payload.get("why") or payload.get("reason") or "").strip()
        kb._append_event(conn, task_id, UNANSWERED_KIND,
                         {"why": question, "because": why_unanswered, "ownership": "agent",
                          "recovery": "retain schedule and answer probe; retry owner delivery"})
    return True


def _latest_chat_park(conn, task_id: str) -> tuple:
    """``(awaiting, reason, wake_check, created_at)`` of the card's newest park when it is a chat wait."""
    import json
    row = conn.execute("SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind IN "
                       "('scheduled', 'continued') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    try:
        payload = json.loads(row[0] or "{}") if row else {}
    except ValueError:
        payload = {}
    record = payload.get("awaiting")
    if not is_chat_wait(record):
        return None, "", None, None
    return record, str(payload.get("reason") or ""), payload.get("wake_check"), int(row[1])


def escalate_if_overdue(conn, task_id: str, *, now: int | None = None) -> bool:
    """Called after a chat wait's comment probe said "not yet" (outside its write txn): a chat park older than
    ``ASK_OVERDUE_S`` with no answer records recovery, never a fabricated human request."""
    import time
    record, _reason, cmd, at = _latest_chat_park(conn, task_id)
    if record is None or at is None:
        return False
    # Every chat wait has the deadline. The card's own comment probe (however spelled: nice, VAR=, a path) just said
    # "not yet", so it is trusted; a probe naming another producer counts an answer since the park as "answered,
    # now waiting on that producer" (adversarial review of #3647).
    trust = is_comment_probe(cmd)
    now = int(time.time()) if now is None else int(now)
    if now - at < ASK_OVERDUE_S:
        return False
    return escalate(conn, task_id, f"it has had no answer for {ASK_OVERDUE_S // 3600} h", trust_probe=trust)


def is_comment_probe(cmd) -> bool:
    """``hermes kanban wait-comment ...`` anywhere in the command's words (after env, VAR=, nice, a full path)."""
    import os
    import shlex
    try:
        words = shlex.split(str(cmd or ""))
    except ValueError:
        return False
    return any(os.path.basename(words[i]) == "hermes" and words[i + 1] == "kanban" and words[i + 2] == "wait-comment"
               for i in range(len(words) - 2))
