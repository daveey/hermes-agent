"""Kernel-made recovery requester for continuation_exhausted parks. [daveey]

The supervisor ``resume`` op (:mod:`hermes_cli.kanban_resume_exec`) only accepts a requester in
the target's creator chain (<= ``MAX_CREATOR_HOPS``) that was created AFTER the target's current
park. A parked card has no live run, so it cannot create that card itself, and every card made
before the park fails the freshness fence by design. Without this module nothing could ever pass
both fences, so the op was unreachable.

Each dispatcher tick, :func:`ensure_recovery_cards` finds home-owned cards whose current park passes
:func:`kanban_resume_exec.exhaustion_refusal` (a continuation_exhausted rekind written by the
refused run, no human gate, no live run, nothing moved it since) and creates ONE recovery card per
park: ``creator_task_id=<target>`` (1 hop), ``created_by=kanban-recovery``, idempotency key
``recovery:<target>:<durable park identity>`` (the local event id on unsynced boards). It is
post-park and in scope by construction, so it may file ``resume`` through the ordinary supervisor
request path; every fence of that path
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

from hermes_cli import kanban_recovery_history as history

logger = logging.getLogger(__name__)

CREATED_BY = "kanban-recovery"
EVENT = "resume_requester_created"


def _key(target: str, park_id) -> str:
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


def _goal_body(target: str, title: str, park_id: int) -> str:
    return (
        f"The kernel judged {target} ({title!r}) goal_unachievable (park event {park_id}). "
        "Investigate its judge receipt, runs and evidence with kanban_show. Diagnose and remedy "
        "the missing prerequisite using existing tools and authorized owners. Preserve the original "
        "body, acceptance criteria, dependency graph, class/tier and capability requirements, authenticated "
        "author exclusions and subscription billing policy. Concrete model/provider restrictions are not "
        "active routing requirements. Do not invent profiles or use paid credentials as a workaround.\n\n"
        "Only after proving the prerequisite repaired, file "
        f'kanban_supervisor_request(ops=[{{"op":"resume","task_id":"{target}"}}], '
        'reason="<repair evidence>"). The non-worker executor rechecks ownership, freshness, human '
        "gates and the two-resume lifetime bound. Read back its receipt and the target status. "
        "Never use raw board mutations, weaken acceptance, or complete/archive "
        "the original. If not repaired, leave it parked and record the concrete unresolved cause; "
        "do not resume speculatively or create another recovery loop. Complete this recovery card "
        "only with the investigation/remediation evidence and resume receipt (or why it remains parked)."
    )


def _candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, title, assignee, tenant, created_by FROM tasks "
        "WHERE status IN ('triage', 'blocked') AND current_run_id IS NULL AND human_request IS NULL "
        "AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id = tasks.id AND e.kind IN ('continuation_exhausted', 'goal_unachievable'))"
    ).fetchall()


def _park_uid(conn: sqlite3.Connection, target: str, park_id: int) -> Optional[str]:
    from hermes_cli.kanban_db_sync import table_columns

    if "uid" not in table_columns(conn, "task_events"):
        return None  # unsynced boards retain the local integer linkage
    row = conn.execute("SELECT uid FROM task_events WHERE task_id = ? AND id = ?",
                       (target, park_id)).fetchone()
    return row[0] if row else None


def _event_for_park(conn: sqlite3.Connection, target: str, park_id: int) -> Optional[str]:
    from hermes_cli.kanban_wake_check import foreign_copy

    park_uid = _park_uid(conn, target, park_id)
    park = conn.execute("SELECT * FROM task_events WHERE task_id = ? AND id = ?", (target, park_id)).fetchone()
    park_identity = history.identity(park) if park is not None and park_uid else None
    foreign = foreign_copy(conn, target)
    for (raw,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC", (target, EVENT)):
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("park_identity"):
            # Authored with the park's causal identity: survives row-loss restoration,
            # which re-inserts the same event under a newer local id.
            matches = park_identity is not None and data["park_identity"] == park_identity
        elif data.get("park_event_uid"):
            matches = data["park_event_uid"] == park_uid
        else:
            # Pre-UID links are only exact at home. A foreign reader cannot disprove
            # ownership using an unrelated local integer: conservatively retain the hold.
            matches = foreign or data.get("park_event_id") == park_id
        if matches:
            return data.get("recovery_task_id")
    return None


def open_recovery_card(conn: sqlite3.Connection, target: str) -> Optional[str]:
    """Open recovery for the current park; foreign pre-UID links conservatively retain their hold.

    Legacy integer links are exact only at home. Triage policy also runs only at home,
    so an ambiguous legacy hold on a retained copy cannot strand home-owned work.
    """
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
    from hermes_cli.kanban_wake_check import foreign_ids, own_gateway

    # A worker-node copy observes the same park with a different local event id.
    # Only the home owns recovery; local idempotency cannot arbitrate across replicas.
    copies = foreign_ids(conn)
    # Requests and their executor are local; a portable requester loses the target
    # when worker-node eviction drops its parked copy. Keep it beside the authority.
    home = own_gateway(conn)
    runs_on = [f"machine:{home}"] if home else None
    made: list[str] = []
    for row in _candidates(conn):
        target = row["id"]
        if target in copies or row["created_by"] == CREATED_BY or rx.exhaustion_refusal(conn, target):
            continue
        park_id = rx._park_event_id(conn, target)
        if _event_for_park(conn, target, park_id) or len(rx._resume_events(conn, target)) >= rx.MAX_RESUMES:
            continue
        try:
            from hermes_cli.kanban_goal_recovery import current_park, recovery_assignee
            task = kb.get_task(conn, target)
            goal = current_park(conn, target)
            from hermes_cli.kanban_gate_only import create_assignee
            # A gate-only target (bb-repair-native) files its recovery under its queue, never the gate's profile.
            assignee = recovery_assignee(task) if goal else create_assignee(row['assignee'] or 'default')
            if not assignee:
                continue
            from hermes_cli.kanban_model_neutral import routing_view
            route = routing_view(task)
            rec = kb.create_task(
                conn, title=f"Recover {target}: {'goal unachievable' if goal else 'continuation budget exhausted'}"[:200],
                body=(_goal_body if goal else _body)(target, row["title"] or "", park_id), assignee=assignee,
                model_override=route.model_override, provider_override=route.provider_override,
                requires=['terminal', 'file', 'kanban'] if goal else None,
                created_by=CREATED_BY, workspace_kind="scratch", tenant=row["tenant"],
                idempotency_key=_key(target, history.park_identity(conn, target) or park_id),
                creator_task_id=target, runs_on=runs_on,
            )
        except (sqlite3.Error, ValueError):
            logger.exception("kanban: recovery card for %s failed", target)
            continue
        payload = {"recovery_task_id": rec, "park_event_id": park_id}
        park_uid = _park_uid(conn, target, park_id)
        if park_uid:
            payload["park_event_uid"] = park_uid
            identity = history.park_identity(conn, target)
            if identity:
                payload["park_identity"] = identity
        with kb.write_txn(conn):
            kb._append_event(conn, target, EVENT, payload)
        made.append(rec)
    return made
