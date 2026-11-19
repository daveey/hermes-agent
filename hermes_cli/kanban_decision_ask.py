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
turns it into a visible ask for David (``needs_input`` with ``human_request``) instead of a silent park.
"""
from __future__ import annotations

from typing import Any

NOTICE_KIND = "decision_requested"
UNANSWERED_KIND = "decision_unanswerable"  # the owner had no chat to take the ask; it became David's


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


def owner_fallback(conn, task_id: str) -> bool:
    """On the owning gateway, after an imported ``decision_requested``: no wakeable subscriber means nobody gets
    the ask, so it becomes David's (:func:`escalate`). True when it blocked."""
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_wake_check as kwc
    if kaw.has_chat_subscriber(conn, task_id):
        return False
    own = kwc.own_gateway(conn)
    return escalate(conn, task_id, f"no chat subscribed to it on {own or 'its owning gateway'} can be woken")


def escalate(conn, task_id: str, why_unanswered: str) -> bool:
    """A still-parked card whose orchestrator ask cannot reach anyone: leave the park (``scheduled`` -> ``triage``,
    probe cleared, ``decision_unanswerable``) and block it ``needs_input`` with the question, as the spend-budget hold
    leaves a park. If the block is refused it stays visibly in triage. True when it blocked."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc
    task = kb.get_task(conn, task_id)
    if task is None or task.status != "scheduled":
        return False
    ask = [e for e in kb.list_events(conn, task_id) if e.kind == NOTICE_KIND]
    payload = (ask[-1].payload or {}) if ask else {}
    question = str(payload.get("why") or payload.get("reason") or "").strip()[:1500]
    question = question or "the decision named in the card's latest comment"
    with kb.write_txn(conn):
        if conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ? AND status = 'scheduled'",
                        (task_id,)).rowcount != 1:
            return False
        kwc.set_check(conn, task_id, None, None)
        kb._append_event(conn, task_id, UNANSWERED_KIND, {"why": question, "because": why_unanswered})
    return bool(kb.block_task(
        conn, task_id, kind="needs_input",
        reason=(f"This card's worker parked waiting on its orchestrator's decision, but {why_unanswered}. "
                "1) Answer the question below with a comment on the card. 2) Then unblock it.\n" + question),
        human_request={
            "action": "Answer the card's question: " + question[:400],
            "why_human": f"its worker asked its orchestrator, and {why_unanswered}",
            "interaction": "reply",
            "scope": f"card {task_id}: {str(task.title or '')[:120]}",
            "evidence": f"decision_requested on {task_id}; {why_unanswered}",
        }))
