"""Who a scoped Kanban worker may archive. [daveey]

Moved out of :mod:`hermes_cli.kanban` (which re-exports every name) to keep that module under
its line cap. A dispatcher worker (``HERMES_KANBAN_TASK`` set) may archive an unclaimed or done
card its own task created, a done or unclaimed capability owner card of its task (or of a card
its task created), or a provably stranded foreign ``runs_on`` copy; every other card is refused.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from hermes_cli import kanban_db as kb


def _creator_task_id(conn, task_id: str) -> Optional[str]:
    """The task that created ``task_id``, from the created-event payload (not a column)."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
        "ORDER BY id ASC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0]).get("creator_task_id")
    except (TypeError, ValueError):
        return None


_TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{16}\b")
_REFILED_RE = re.compile(r"\bre-?filed\b", re.IGNORECASE)


def _is_stranded_runs_on_copy(conn, task) -> bool:
    """A foreign card no machine will ever run, already re-filed elsewhere. [daveey]

    All must hold: ``ready``; no ``claimed`` event ever; ``runs_on`` has at least one
    ``machine:<m>`` label and every such ``<m>`` is another gateway that
    ``placement_runs_on`` refuses today (no advertised pickup), read from a reachable
    machine registry; and a comment says it was re-filed, naming another task id.
    """
    from hermes_cli import kanban_db_sync as ksync
    from hermes_cli import kanban_worker_node as wn

    if task.status != "ready":
        return False
    if conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'claimed' LIMIT 1",
                    (task.id,)).fetchone():
        return False
    try:
        if "runs_on" not in ksync.table_columns(conn, "tasks"):
            return False
        row = conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (task.id,)).fetchone()
        machines = [label.split(":", 1)[1] for label in (wn._parse(row[0] if row else None) or [])
                    if label.startswith("machine:")]
        if not machines or wn.registry() is None:
            return False
        own = wn._own(conn)
        if any(not m or m == own or wn.placement_runs_on(conn, m) for m in machines):
            return False
    except Exception:  # health: allow BLE001 -- unreadable sync state: not provably stranded, refuse
        return False
    for (body,) in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (task.id,)):
        body = body or ""
        if _REFILED_RE.search(body) and any(t != task.id for t in _TASK_ID_RE.findall(body)):
            return True
    return False


def worker_archive_scope(conn, task_id: str) -> tuple[Optional[str], Optional[str]]:
    """``(refusal, via)`` for archiving ``task_id`` from this process.

    ``refusal`` is None when allowed. ``via`` is set when the archive is allowed only
    as a stranded-``runs_on`` sweep of a foreign card; record it on the archived event.
    """
    # Terminal children retain the worker marker rather than the tool-only task marker.
    worker_task = os.environ.get("HERMES_KANBAN_TASK") or os.environ.get("HERMES_KANBAN_WORKER_TASK")
    return archive_scope_for(conn, task_id, worker_task)


def archive_scope_for(conn, task_id: str, env_tid: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """:func:`worker_archive_scope` for a worker scoped to ``env_tid`` (None: unscoped, any card).

    The home gateway applies it to an archive a Board worker node forwarded for a relayed capability owner
    card (:mod:`hermes_cli.kanban_owner_archive_relay`), where the scope comes from the request, not the env.
    """
    if not env_tid:
        return None, None
    if env_tid == task_id:
        return ("refusing to archive the assigned task; finish it with kanban_complete"), None
    task = kb.get_task(conn, task_id)
    if task is None:
        return f"no such task: {task_id}", None
    waiting = _capability_owner_waiting_card(task)
    if waiting and (waiting == env_tid or _creator_task_id(conn, waiting) == env_tid):
        return _worker_may_archive_capability_owner(task, waiting), f"capability owner of {waiting} by {env_tid}"
    if _creator_task_id(conn, task_id) != env_tid:
        if not (task.claim_lock or task.worker_pid) and _is_stranded_runs_on_copy(conn, task):
            return None, f"stranded-runs_on sweep by {env_tid}"
        return (f"worker is scoped to task {env_tid}; refusing to archive {task_id}. "
                "A worker may archive only an unclaimed or done card its own task created."), None
    return _worker_may_archive_own(conn, task), None


def worker_may_archive(conn, task_id: str) -> Optional[str]:
    """None if this process may archive ``task_id``, else the refusal.

    A dispatcher worker may archive an unclaimed card its own task created
    (``creator_task_id`` is ``HERMES_KANBAN_TASK``), or a done one with no open card
    listing it as a parent, or a foreign card that is a
    provably stranded ``runs_on`` copy (see ``_is_stranded_runs_on_copy``), or a done or
    unclaimed capability owner card (``capability-owner:<card>:...``) whose ``<card>`` is
    ``HERMES_KANBAN_TASK`` or a card that task created. A reviewer of that task runs with the
    same ``HERMES_KANBAN_TASK``, so it may archive them too. Every
    other foreign card is refused — that is the "scoped worker can't archive it"
    wall. No env task (a human or an orchestrator) may archive any card, as before.
    """
    return worker_archive_scope(conn, task_id)[0]


def _capability_owner_waiting_card(task) -> Optional[str]:
    """The waiting card of a capability owner card (``capability-owner:<card>:<need>``), else None.

    Owner cards are created by the routing path (``created_by="capability-relay"`` for a
    relayed local owner), never by the waiting card's task, so the creator rule alone
    never lets that task clean them up. [daveey]
    """
    from hermes_cli.kanban_capability_routing import _task_of

    return _task_of(task.idempotency_key or "")


def _worker_may_archive_capability_owner(task, waiting: str) -> Optional[str]:
    """The refusal for an owner card of ``waiting`` (the worker's task or a card it created), else None.

    A done owner card may be archived (the readback is in; this is the cleanup), and so may an
    unclaimed one. A claimed owner card is refused: its worker is doing the action.
    """
    if task.status == "archived":
        return f"cannot archive {task.id}: status=archived"
    if task.status != "done" and (task.status == "running" or task.claim_lock or task.worker_pid):
        return (f"cannot archive {task.id}: capability owner card of {waiting} is claimed "
                f"(status={task.status}). A worker may archive only a done or unclaimed owner card.")
    return None


def _worker_may_archive_own(conn, task) -> Optional[str]:
    """The refusal for a card this worker's task created, else None.

    An unclaimed card may be archived (the misplaced-copy cleanup), and so may a done one with
    no live claim and no open card waiting on it: a scratch waiter is claimed and completed by
    its own worker, so without this its creator could never clean it up. [daveey]
    """
    task_id = task.id
    if task.status == "archived":
        return f"cannot archive {task_id}: status=archived"
    if task.status == "running" or task.claim_lock or task.worker_pid:
        return (f"cannot archive {task_id}: it is claimed (status={task.status}). "
                "A worker may archive only an unclaimed or done card its own task created.")
    if task.status == "done":
        open_children = [row[0] for row in conn.execute(
            "SELECT l.child_id FROM task_links l JOIN tasks c ON c.id = l.child_id "
            "WHERE l.parent_id = ? AND c.status NOT IN ('done', 'archived') ORDER BY l.child_id",
            (task_id,))]
        if open_children:
            return (f"cannot archive {task_id}: open card(s) {', '.join(open_children)} list it as a parent. "
                    "Archive it once they finish.")
    return None
