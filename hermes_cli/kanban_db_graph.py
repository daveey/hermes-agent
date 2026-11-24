"""Task graph initialization and atomic decomposition persistence."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional


def note_missing_parents(
    conn: sqlite3.Connection, task_id: str, states: list[tuple[str, Optional[str]]], *,
    remote: bool = False, fresh: Optional[dict] = None,
) -> bool:
    """Track a missing-parent streak inside the home gateway's readiness transaction.

    On a synced board (``remote``) a parent with no status is unknown, not missing, unless the mirror's replica is
    at the feed head and, on a primary board, the mirror was refreshed from that pull
    (``kanban_replica_freshness.mirror_at_head``, read once per readiness pass into ``fresh``):
    otherwise no streak event is recorded and nothing is blocked. The child still waits: an unknown parent gates.
    Each deferral is counted in ``fresh["deferred"]`` and the pass logs one INFO line per board per stale replica
    stamp (``kanban_replica_freshness.report_deferred``). A replica that never catches up is alarmed in awake time
    by the sync-freshness check of t_01a1d3df5e5d0757, not by a wall-clock bound here: a laptop's stamp is hours
    old on wake from sleep, so such a bound would fire at once and bring back the false blocks.
    A streak is scoped (``_streak_open``): a parent missing after an unblock or a link, or one the open streak does
    not name, starts a fresh ``parent_missing`` and waits its own :data:`PARENT_MISSING_BLOCK_SECONDS`."""
    from hermes_cli import kanban_db as kb

    if not states:
        return False
    missing = [pid for pid, st in states if st is None]
    if missing and remote:
        from hermes_cli.kanban_replica_freshness import mirror_at_head, synced
        cache = {} if fresh is None else fresh
        if "fresh" not in cache:  # a detached board (sync off) keeps its mirror table but is its own authority
            cache["fresh"] = mirror_at_head(conn) if synced(conn) else True
        if not cache["fresh"]:
            cache["deferred"] = cache.get("deferred", 0) + 1
            return False
    last = conn.execute(
        "SELECT id, kind, created_at, payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('parent_missing', 'parent_found') ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if not missing:
        if last is not None and last["kind"] == "parent_missing":
            kb._append_event(conn, task_id, "parent_found", None)
        return False
    if not _streak_open(conn, task_id, last, missing):
        kb._append_event(conn, task_id, "parent_missing", {"parents": missing})
        return False
    if int(kb.time.time()) - int(last["created_at"] or 0) < kb.PARENT_MISSING_BLOCK_SECONDS:
        return False
    reason = "; ".join(f"parent {pid} missing from this board" for pid in missing)
    cur = conn.execute(
        "UPDATE tasks SET status = 'blocked', block_kind = 'transient' WHERE id = ? AND status = 'todo'",
        (task_id,),
    )
    if cur.rowcount != 1:
        return False
    repair_id = missing_parent_repair(conn, task_id, missing)
    kb._append_event(conn, task_id, "blocked", {
        "reason": reason + f"; repair card {repair_id} owns investigation and recovery",
        "kind": "transient", "source": "parent_missing", "parents": missing,
        "repair_task_id": repair_id,
    })
    return True


# The newest of these on a blocked child must be the detector's own block for it to be cleared automatically.
_LIFECYCLE_EVENTS = ("blocked", "unblocked", "gave_up", "block_loop_detected", "dependency_wait", "status",
                     "supervisor_escalated", "changes_requested", "review_reopened")


def clear_found_parents(conn: sqlite3.Connection, row, *, remote: bool) -> str:
    """Inside the readiness transaction: return a child the detector blocked (:func:`note_missing_parents`) to
    ``todo`` once :func:`kanban_db.parent_states` shows every parent again (the local row synced in, or the
    remote-parent mirror now has it). Returns the readiness ``row``'s status after the call (``todo`` when it
    cleared the block); the caller then runs its ``todo`` path, which writes ``parent_found`` and promotes the
    child when every parent is done.

    Only the detector's own block is cleared: the child's newest lifecycle event must be the ``blocked`` with
    ``source`` 'parent_missing', with no human request on the card. A later hand, policy or
    breaker block, or a block from any other source, stays for its owner. A child left with no parent link at all
    also clears (nothing is missing): that unblock's ``source`` is ``parents_unlinked``, since no parent came back
    and no ``parent_found`` follows. The repair card is left to finish, with a system comment (while still open)
    saying which of the two cleared the block.
    2026-11-13, David-MBP: t_01a1d0ea6db6c158 stayed blocked hours after its parent was mirrored done. [daveey]"""
    from hermes_cli import kanban_db as kb

    task_id, status = row["id"], row["status"]
    if status != "blocked":
        return status
    last = conn.execute(
        f"SELECT id, payload FROM task_events WHERE task_id = ? AND kind IN "
        f"({', '.join('?' * len(_LIFECYCLE_EVENTS))}) ORDER BY id DESC LIMIT 1", (task_id, *_LIFECYCLE_EVENTS),
    ).fetchone()
    if last is None or _event_field(last["payload"], "source") != "parent_missing":  # only the detector's block
        return status
    # No parent left (every link removed while blocked) counts as resolved: nothing is missing any more.
    states = kb.parent_states(conn, task_id, remote=remote)
    if any(st is None for _pid, st in states):
        return status
    cur = conn.execute(
        "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'blocked' AND human_request IS NULL", (task_id,),
    )
    if cur.rowcount != 1:
        return status
    repair_id = _event_field(last["payload"], "repair_task_id")
    # PR #2192 review: with no link left note_missing_parents writes no parent_found, so say what really happened.
    source = "parent_found" if states else "parents_unlinked"
    kb._append_event(conn, task_id, "unblocked", {
        "status": "todo", "source": source, "cleared_block": "parent_missing",
        "blocked_event_id": last["id"], "parents": _event_field(last["payload"], "parents"),
        "repair_task_id": repair_id,
    })
    _tell_repair_card(conn, repair_id, task_id, last["id"], source)
    return "todo"


def _tell_repair_card(conn: sqlite3.Connection, repair_id, task_id: str, blocked_event_id: int, source: str) -> None:
    """In the caller's txn: note on the still-open repair card that its child's block cleared, so its worker does
    not investigate a parent that is back. A finished or absent repair card gets nothing. The note is a system
    comment (no writer run), so the repair card's own worker receives it even when this pass ran inside it. [daveey]"""
    from hermes_cli import kanban_db as kb

    if not isinstance(repair_id, str) or kb._task_status(conn, repair_id) in (None, "done", "archived"):
        return
    why = ("every parent came back (parent_found)" if source == "parent_found"
           else "every parent link was removed while it was blocked (parents_unlinked); no parent came back")
    kb.add_comment(conn, repair_id, "kanban-dispatcher", (
        f"The parent_missing block on {task_id} (blocked_event_id {blocked_event_id}) cleared: {why}, "
        "so the child is back in todo. Nothing is left to recover for that block; confirm and close this card."
    ), system=True)


def report_deferred_pass(conn: sqlite3.Connection, fresh: dict) -> None:
    """After a readiness pass: the children :func:`note_missing_parents` deferred because the replica is not at the
    feed head get one INFO line per board per stale replica stamp (``kanban_replica_freshness.report_deferred``)."""
    if fresh.get("deferred"):
        from hermes_cli.kanban_replica_freshness import report_deferred
        report_deferred(conn, fresh["deferred"])


def _streak_open(conn: sqlite3.Connection, task_id: str, last, missing: list[str]) -> bool:
    """The newest ``parent_missing`` is this streak only when no ``unblocked`` event, nor a ``linked`` event for a
    parent missing now (or naming none), follows it, and it names every parent missing now: a later incident must
    not inherit an earlier one's elapsed time, and a link to a present parent does not restart a missing one's."""
    if last is None or last["kind"] != "parent_missing":
        return False
    for kind, payload in conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? AND id > ? AND kind IN ('unblocked', 'linked')",
        (task_id, last["id"]),
    ).fetchall():
        if kind == "unblocked" or _event_field(payload, "parent") in (None, *missing):
            return False
    named = _event_field(last["payload"], "parents")
    return isinstance(named, list) and set(missing) <= set(named)


def _event_field(payload, key: str):
    """``key`` of a JSON event payload; None when absent or unreadable."""
    try:
        return (json.loads(payload or "{}") or {}).get(key)
    except (TypeError, ValueError, AttributeError):
        return None


def missing_parent_repair(conn: sqlite3.Connection, task_id: str, missing: list[str]) -> str:
    """Compose repair admission in the readiness transaction, once per open incident."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_triage_sweep import _supervisor_profile
    from hermes_cli.kanban_model_neutral import forwarded_text
    existing = conn.execute(
        "SELECT t.id FROM task_events e JOIN tasks t "
        "ON t.id = json_extract(e.payload, '$.repair_task_id') "
        "WHERE e.task_id = ? AND e.kind = 'blocked' "
        "AND json_extract(e.payload, '$.source') = 'parent_missing' "
        "AND t.status NOT IN ('done', 'archived') ORDER BY e.id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if existing is not None:
        return existing['id']
    # create_task opts into a savepoint. Failure rolls back the child's block
    # too, leaving the next readiness tick free to retry the whole transition.
    task = conn.execute("SELECT title, tenant FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return kb.create_task(
        conn, title=f"Recover missing dependencies for {task_id}",
        body=(f"Card {task_id} has waited at least "
              f"{kb.PARENT_MISSING_BLOCK_SECONDS} seconds for unknown parent(s): "
              + ", ".join(missing) + ".\n\nSource title (historical evidence):\n"
              + forwarded_text(task['title']) + "\n\n"
              "Inspect the owning gateway, full Board replica and authoritative Board row evidence. "
              "A missing local row is not proof of Board loss; check sync freshness and pickup eligibility. "
              "Recover missing publication or dependency state, then unblock the child with the kanban_unblock "
              "tool: it works across gateways (when the child lives on another gateway it forwards the unblock "
              "as an operator call, which the child's home applies) and records the unblock as this card's. "
              "Never run the hermes CLI over ssh on the child's home gateway: that unblock is recorded as "
              "David's. "
              "Do not unlink a required dependency merely to make it run. Record evidence and verify "
              "the child resumes. This is agent-owned repair, not a human decision."),
        assignee=_supervisor_profile(), created_by="missing-parent-detector",
        workspace_kind="scratch", tenant=task['tenant'],
        requires=["terminal", "file", "kanban"],
        creator_task_id=task_id,
    )


def inherit_creator_origin(
    conn: sqlite3.Connection, task_id: str, creator_task_id: Optional[str], *,
    created_at: int,
) -> None:
    """Copy durable origin inside creation's transaction, never adding dependencies."""
    if not creator_task_id:
        return
    from hermes_cli.kanban_db import _inherit_notify_subs

    conn.execute(
        "UPDATE tasks SET session_id = COALESCE(session_id, "
        "(SELECT session_id FROM tasks WHERE id = ?)) WHERE id = ?",
        (creator_task_id, task_id),
    )
    _inherit_notify_subs(conn, task_id, (creator_task_id,), created_at=created_at)
    from hermes_cli.kanban_creator_chats import inherit_chats
    inherit_chats(conn, task_id, creator_task_id)


def initial_task_state(
    conn: sqlite3.Connection, parents: tuple[str, ...], initial_status: str,
    triage: bool, tenant: Optional[str],
) -> tuple[str, Optional[str]]:
    """Resolve state and tenant under the creator's write transaction.

    Parent order breaks ties in this soft namespace; explicit tenant wins.
    Validate parents even for parked tasks so links never dangle.
    """
    rows = {}
    if parents:
        rows = {row["id"]: row for row in conn.execute(
            "SELECT id, status, tenant FROM tasks WHERE id IN "
            "(" + ",".join("?" * len(parents)) + ")", parents,
        )}
        missing = [pid for pid in parents if pid not in rows]
        if missing:
            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
        if tenant is None:
            tenant = next((rows[pid]["tenant"] for pid in parents if rows[pid]["tenant"]), None)
    if initial_status == "blocked":
        return "blocked", tenant
    if triage:
        return "triage", tenant
    if any(row["status"] != "done" for row in rows.values()):
        return "todo", tenant
    return "ready", tenant


def _validate_children_graph(children: list) -> None:
    """DB-free shape check + Kahn's cycle check on the sibling graph (a cycle
    would deadlock every involved child in ``todo`` forever)."""
    for idx, child in enumerate(children):
        if not isinstance(child, dict):
            raise ValueError(f"child[{idx}] is not a dict")
        title = child.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"child[{idx}].title is required")
        parents_idx = child.get("parents") or []
        if not isinstance(parents_idx, list):
            raise ValueError(f"child[{idx}].parents must be a list")
        for p in parents_idx:
            if not isinstance(p, int) or p < 0 or p >= len(children):
                raise ValueError(f"child[{idx}].parents[{p}] is not a valid index into children")
            if p == idx:
                raise ValueError(f"child[{idx}] cannot list itself as a parent")

    in_deg = [0] * len(children)
    adj: list[list[int]] = [[] for _ in children]
    for i, c in enumerate(children):
        for p in (c.get("parents") or []):
            adj[p].append(i)
            in_deg[i] += 1
    queue = [i for i in range(len(children)) if in_deg[i] == 0]
    seen = 0
    while queue:
        seen += 1
        for nb in adj[queue.pop()]:
            in_deg[nb] -= 1
            if in_deg[nb] == 0:
                queue.append(nb)
    if seen != len(children):
        raise ValueError("cyclic dependency detected in decomposed children list")


def decompose_triage_task(
    conn: sqlite3.Connection, task_id: str, *, root_assignee: Optional[str], children: list[dict],
    author: Optional[str] = None, auto_promote: bool = True, require_declaration: bool = False,
) -> Optional[list[str]]:
    """Fan a triage task out into children and move the root to ``todo``; the root
    waits on every child and wakes (``ready``) when all are done.

    ``children``: dicts of ``title`` (required), ``body``, ``assignee``,
    ``parents`` (indices into this list), optional workspace overrides.
    Returns child ids in input order, or None when the root is missing / not
    in triage, or has already decomposed. Atomic: malformed entries abort fan-out.
    ``require_declaration``: a child without a ``requires`` entry that no workspace backs raises
    ``RequiresDeclarationMissing`` (the whole fan-out is refused; nothing is written). [daveey]
    """
    from hermes_cli.kanban_db import (
        _canonical_assignee, _link, _append_event, _insert_comment,
        write_txn, recompute_ready,
    )

    if not children:
        return None
    from hermes_cli.kanban_gate_only import create_assignee
    if root_assignee is not None:
        root_assignee = create_assignee(_canonical_assignee(root_assignee))  # gate-only -> its queue [daveey]
    _validate_children_graph(children)

    # ONE txn so the fan-out is atomic; helpers that open their own write_txn
    # (create_task, link_tasks, add_comment) must not be called in here.
    now = int(time.time())
    with write_txn(conn):
        root_row = conn.execute(
            "SELECT id, status, tenant, workspace_kind, workspace_path, assignee, requires "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if root_row is None or root_row["status"] != "triage":
            return None
        # Dependency links alone do not imply lineage. The completion event is
        # committed with the graph, and survives re-triage or unlinking.
        if conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'decomposed' LIMIT 1",
            (task_id,),
        ).fetchone():
            return None
        import json
        from hermes_cli.kanban_capabilities import validate_assignment
        validate_assignment(
            root_assignee if root_assignee is not None else root_row["assignee"],
            requires=json.loads(root_row["requires"] or "[]"),
            workspace_kind=root_row["workspace_kind"], workspace_path=root_row["workspace_path"],
        )
        child_ids = [
            _insert_decomposed_child(conn, task_id, root_row, child, author, now,
                                     require_declaration=require_declaration, index=idx)
            for idx, child in enumerate(children)
        ]
        # Sibling edges within the decomposed graph.
        for idx, child in enumerate(children):
            for p_idx in child.get("parents") or []:
                parent_id, child_id = child_ids[p_idx], child_ids[idx]
                _link(conn, parent_id, child_id)
                _append_event(conn, child_id, "linked", {"parent": parent_id, "child": child_id})
        # Root waits for the whole graph: link it under EVERY child (simpler
        # than computing leaves; cycle-free since the root is only ever a child).
        for cid in child_ids:
            _link(conn, cid, task_id)
        # Flip the root triage -> todo, assignee -> orchestrator.
        sets = ["status = 'todo'"]
        params: list[Any] = []
        if root_assignee is not None:
            sets.append("assignee = ?")
            params.append(root_assignee)
        params.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(params))
        if author and author.strip():
            _insert_comment(
                conn, task_id, author.strip(),
                "Decomposed into " + ", ".join(child_ids)
                + ". Root will wake when all children complete.",
                now,
            )
        _append_event(
            conn, task_id, "decomposed", {"child_ids": child_ids, "root_assignee": root_assignee},
        )
    # Outside the txn (own IMMEDIATE txn). ``auto_promote=False`` leaves the
    # children in ``todo`` for manual-review-first workflows.
    if auto_promote:
        recompute_ready(conn)
    return child_ids


def _insert_decomposed_child(
    conn: sqlite3.Connection, root_id: str, root_row: sqlite3.Row, child: dict,
    author: Optional[str], now: int, *, require_declaration: bool = False, index: int = 0,
) -> str:
    """Insert one decomposed child as ``todo`` (linked under the root later so
    the dispatcher only ever sees a coherent graph); returns its id.

    Workspace: per-child override wins, else inherit the root's kind. Path
    inherits only when kinds match (a 'dir' child must not point at the
    root's worktree) and NEVER for worktrees — siblings dispatch concurrently
    and one shared checkout would put them all on the first sibling's branch
    with no lock; leaving it unset makes dispatch materialize a fresh
    ``<repo>/.worktrees/<child-id>`` per child from the board anchor.
    """
    from hermes_cli.kanban_db import _canonical_assignee, _append_event
    from hermes_cli.kanban_db_sync import new_task_id

    root_ws_kind = root_row["workspace_kind"] or "scratch"
    child_ws_kind = child.get("workspace_kind") or root_ws_kind
    if child.get("workspace_path"):
        child_ws_path = child.get("workspace_path")
    elif child_ws_kind == "worktree":
        child_ws_path = None
    elif child_ws_kind == root_ws_kind:
        child_ws_path = root_row["workspace_path"]
    else:
        child_ws_path = None
    new_id = new_task_id(conn)
    import json
    from hermes_cli.kanban_capabilities import check_declaration, validate_assignment
    from hermes_cli.kanban_gate_only import create_assignee
    # A fan-out child is a fresh card: a gate-only pick files under its queue, as a create would. [daveey]
    assignee = create_assignee(_canonical_assignee(child.get("assignee")))
    if require_declaration:
        check_declaration(child.get("requires"), child_ws_kind, assignee=assignee,
                          where=f"tasks[{index}].requires")
    required = validate_assignment(assignee, requires=child.get("requires"),
                                   workspace_kind=child_ws_kind, workspace_path=child_ws_path)
    body = child.get("body")
    from hermes_cli.kanban_admission_score import effective_priorities

    priority = effective_priorities(conn, [root_id]).get(root_id, 0)
    conn.execute(
        "INSERT INTO tasks "
        "(id, title, body, assignee, status, workspace_kind, "
        " workspace_path, tenant, created_at, created_by, requires, priority) "
        "VALUES (?, ?, ?, ?, 'todo', ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id, child["title"].strip(), body if isinstance(body, str) else None,
            assignee, child_ws_kind, child_ws_path,
            root_row["tenant"], now, (author or "decomposer"),
            json.dumps(required), priority,
        ),
    )
    _append_event(
        conn, new_id, "created", {"by": author or "decomposer", "from_decompose_of": root_id,
                                  "requires": required, "priority": priority},
    )
    inherit_creator_origin(conn, new_id, root_id, created_at=now)
    from hermes_cli.kanban import _agent_invoked
    if _agent_invoked():
        from hermes_cli.kanban_cli_body import session_identity
        from hermes_cli.kanban_create_authority import require
        import os
        _, session = session_identity(bool(os.environ.get('HERMES_KANBAN_TASK')))
        conn.execute('UPDATE tasks SET session_id=COALESCE(session_id,?) WHERE id=?', (session, new_id))
        require(conn, new_id, None)  # a triaged decomposition root is not an admitted creator run
    return new_id
