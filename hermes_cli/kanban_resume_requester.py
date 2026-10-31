"""Kernel-made recovery requester for continuation_exhausted parks. [daveey]

The supervisor ``resume`` op (:mod:`hermes_cli.kanban_resume_exec`) only accepts a requester in
the target's creator chain (<= ``MAX_CREATOR_HOPS``) that was created AFTER the target's current
park. A parked card has no live run, so it cannot create that card itself, and every card made
before the park fails the freshness fence by design. Without this module nothing could ever pass
both fences, so the op was unreachable.

Each dispatcher tick, :func:`ensure_recovery_cards` finds cards whose current park passes
:func:`kanban_resume_exec.exhaustion_refusal` (a continuation_exhausted rekind written by the
refused run, no human gate, no live run, nothing moved it since) and creates ONE recovery card per
park: ``creator_task_id=<target>`` (1 hop), ``created_by=kanban-recovery``, idempotency key
``recovery:<target>:<park blocked event id>``. It is post-park and in scope by construction, so it
may file ``resume`` through the ordinary supervisor request path; every fence of that path
(scope, freshness, human gate, exhaustion, ``MAX_RESUMES``) still runs unchanged at execution.

Not created: for a card whose resume budget is spent (the request would be refused), for a card
that is itself a kernel recovery card (no recursion), or twice for the same park. While the
recovery card is open the stale-triage sweep leaves the target alone (:func:`open_recovery_card`):
that card is its producer.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

logger = logging.getLogger(__name__)

CREATED_BY = "kanban-recovery"
EVENT = "resume_requester_created"


def _key(target: str, park_id: int) -> str:
    return f"recovery:{target}:{park_id}"


def _body(target: str, title: str, park_id: int) -> str:
    return (
        f"Card {target} ({title!r}) used up its consecutive-continuation budget and the kernel parked it "
        f"(continuation_exhausted, park event {park_id}). It has no run, and no worker can unblock it. "
        "This card was created by the kernel for that park, so it is the one card allowed to ask for a "
        "supervisor resume of it.\n\n"
        f"1. Read {target}: its last checkpoints, comments and runs (`kanban_show(task_id='{target}')`). "
        "Decide whether it was making progress and only needs more runs, or is looping.\n"
        "2. Making progress: file `kanban_supervisor_request(ops=[{\"op\": \"resume\", \"task_id\": "
        f"\"{target}\"}}], reason=\"<evidence it is progressing>\")`. The non-worker executor re-checks the "
        "park and applies it; read the receipt back with `kanban_show` (supervisor_requests) and confirm "
        f"{target} is ready or todo.\n"
        f"3. Looping or too big for one card: do not resume. Split the remaining work into child cards "
        f"(`kanban_create`, parents as needed) and record on {target} why it was split.\n"
        f"4. Comment on {target} with what you did and the request receipt, then complete this card.\n\n"
        "A human gate (needs_input / human_request) on the target is never resumed by this path."
    )


def _candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, title, assignee, tenant, created_by FROM tasks "
        "WHERE status IN ('triage', 'blocked') AND current_run_id IS NULL AND human_request IS NULL "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = tasks.id AND e.kind = 'continuation_exhausted')"
    ).fetchall()


def _event_for_park(conn: sqlite3.Connection, target: str, park_id: int) -> Optional[str]:
    for (raw,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC", (target, EVENT)):
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("park_event_id") == park_id:
            return data.get("recovery_task_id")
    return None


def open_recovery_card(conn: sqlite3.Connection, target: str) -> Optional[str]:
    """The kernel recovery card for ``target``'s CURRENT park while it is still open, else None."""
    from hermes_cli import kanban_resume_exec as rx
    rec = _event_for_park(conn, target, rx._park_event_id(conn, target))
    if not rec:
        return None
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (rec,)).fetchone()
    return rec if row and row[0] not in ("done", "archived") else None


def ensure_recovery_cards(conn: sqlite3.Connection) -> list[str]:
    """Create the missing recovery card for every resumable continuation_exhausted park. Returns new ids."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_resume_exec as rx

    made: list[str] = []
    for row in _candidates(conn):
        target = row["id"]
        if row["created_by"] == CREATED_BY or rx.exhaustion_refusal(conn, target):
            continue
        park_id = rx._park_event_id(conn, target)
        if _event_for_park(conn, target, park_id) or len(rx._resume_events(conn, target)) >= rx.MAX_RESUMES:
            continue
        try:
            rec = kb.create_task(
                conn, title=f"Recover {target}: continuation budget exhausted"[:200],
                body=_body(target, row["title"] or "", park_id), assignee=row["assignee"] or "default",
                created_by=CREATED_BY, workspace_kind="scratch", tenant=row["tenant"],
                idempotency_key=_key(target, park_id), creator_task_id=target,
            )
        except (sqlite3.Error, ValueError):
            logger.exception("kanban: recovery card for %s failed", target)
            continue
        with kb.write_txn(conn):
            kb._append_event(conn, target, EVENT, {"recovery_task_id": rec, "park_event_id": park_id})
        made.append(rec)
    return made
