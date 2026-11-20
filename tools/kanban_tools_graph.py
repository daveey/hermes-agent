"""Dependency-graph tools: ``kanban_link``, ``kanban_unlink``, ``kanban_supervisor_request``.

Lives outside ``kanban_tools`` so that file, already over its line cap, does not grow.
``kanban_tools`` wraps these with ``_kanban_handler`` and registers them, so the tool
names, schemas and error shapes are unchanged. [daveey]

Ownership: ``kanban_unlink`` lets a dispatcher worker remove only its own card's edges.
Edges between cards the worker's task created go through ``kanban_supervisor_request``,
a durable request that the board's non-worker executor (the dispatcher tick) applies
with these same handlers (``hermes_cli.kanban_supervisor_requests``).
"""
from __future__ import annotations

import os


def handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact (cycles/self-links/running
    children → ValueError). A worker linking its OWN running card proves ownership
    with its run id so the dependency-block handoff still works."""
    from tools.kanban_tools import (
        _board, _check, _maybe_subscribe_on_touch, _ok, _reject_delegated_child_mutation, _worker_run_id)

    _reject_delegated_child_mutation("kanban_link", args)
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    _check(parent_id and child_id, "both parent_id and child_id are required")
    from tools import kanban_tools_run_end as kre
    with _board(args.get("board")) as (kb, conn):
        own = os.environ.get("HERMES_KANBAN_TASK")
        if own in (str(parent_id), str(child_id)):  # a parked worker must not edit its own card's edges
            refused = kre.refusal(conn, "kanban_link", own)
            if refused:
                return refused
        # Graph tools carry two endpoints, not the task_id checked by other writes.
        from hermes_cli import kanban_foreign_card as kfc
        from tools.registry import tool_error
        for endpoint in (child_id, parent_id):
            foreign = kfc.refusal(conn, str(endpoint))
            if foreign:
                return tool_error(
                    foreign + " Do not retry this local link or import a replica card. "
                    "Read both endpoints with cross_task to verify their current owners and existing edges. "
                    "If the child is local and still needs a remote parent, park the child with "
                    "kind='time', awaiting={kind:'card', ref:'" + str(parent_id) + "', "
                    "gateway:'<verified-parent-owner>'}. If the child is remote, ask its owner "
                    "by comment to arrange the dependency; do not park a different card here.",
                    rejection=kfc.REJECTION)
        gated = kb.link_tasks(
            conn, parent_id=parent_id, child_id=child_id,
            expected_child_run_id=_worker_run_id(str(child_id)))
        _maybe_subscribe_on_touch(conn, str(child_id))
        return _ok(parent_id=parent_id, child_id=child_id, gated=gated,
                   **({"gated_by": parent_id} if gated else {}))


def handle_unlink(args: dict, **kw) -> str:
    """Remove a dependency edge. A dispatcher worker edits only its OWN card's edges (it is
    the child or the parent); orchestrators any."""
    from tools.kanban_tools import _board, _check, _ok, _Reject, _reject_delegated_child_mutation

    _reject_delegated_child_mutation("kanban_unlink", args)
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    _check(parent_id and child_id, "both parent_id and child_id are required")
    parent_id, child_id = str(parent_id), str(child_id)
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and env_tid not in (parent_id, child_id):
        raise _Reject(f"kanban_unlink refused: this worker owns {env_tid}, not the edge "
                      f"{parent_id} -> {child_id}; it may only unlink edges of its own card. To remove "
                      "edges between cards your task created, ask the board's supervisor executor: "
                      "kanban_supervisor_request(ops=[{\"op\": \"unlink\", \"parent_id\": ..., "
                      "\"child_id\": ...}]) (via tool_call; dry_run=true previews scope)")
    from tools import kanban_tools_run_end as kre
    with _board(args.get("board")) as (kb, conn):
        refused = kre.refusal(conn, "kanban_unlink")  # unlinking a dependency park's parent undid the park
        if refused:
            return refused
        _check(kb.unlink_tasks(conn, parent_id, child_id), f"no such link: {parent_id} -> {child_id}")
        return _ok(parent_id=parent_id, child_id=child_id, unlinked=True)


def handle_supervisor_request(args: dict, **kw) -> str:
    """File a durable graph-edit request for the non-worker supervisor executor."""
    from hermes_cli import kanban_supervisor_requests as ksr
    from tools.kanban_tools import (
        _board, _is_dispatcher_owned_worker, _ok, _parse_bool_arg, _redact_opt, _Reject,
        _reject_delegated_child_mutation)
    from tools.registry import tool_error

    _reject_delegated_child_mutation("kanban_supervisor_request")
    if not _is_dispatcher_owned_worker():
        raise _Reject("kanban_supervisor_request is for dispatcher workers; an orchestrator or operator "
                      "may use kanban_link / kanban_unlink on any edge directly", "not_a_worker")
    with _board(args.get("board")) as (_kb, conn):
        try:
            out = ksr.submit(
                conn, env_task=os.environ.get("HERMES_KANBAN_TASK"),
                env_run=os.environ.get("HERMES_KANBAN_RUN_ID"), ops=args.get("ops"),
                reason=_redact_opt(args.get("reason")), dry_run=_parse_bool_arg(args, "dry_run"))
        except ksr.SupervisorRequestError as exc:
            return tool_error(str(exc), code=exc.code, handoff_complete=False)
    if not out.get("dry_run"):
        out["next_step"] = (
            f"Supervisor request #{out['request_id']} is {out['status']}. It is agent-owned, not a human "
            "ask. Park this card with kanban_schedule(reason='SCHEDULED_UNTIL=<ISO8601> waiting on "
            f"supervisor request #{out['request_id']}', awaiting={{kind:'external', ref:'supervisor request "
            f"#{out['request_id']}', why:'the supervisor executor runs it'}}); the result wakes it, also if it "
            "lands first.")
        if ksr._is_operator(out.get("ops") or []):
            out["next_step"] += (
                " An operator call is executed by the card's home gateway; add wake_check="
                f"'hermes kanban supervisor-requests --id {out['request_id']} --settled' so a worker node keeps "
                "this card's copy until the answer arrives.")
    return _ok(**out)


def supervisor_requests(conn, tid: str) -> list[dict]:
    """The latest requests ``tid`` filed, for ``kanban_show``."""
    from hermes_cli import kanban_supervisor_requests as ksr
    from tools.kanban_tools import logger
    try:
        return [{k: r.get(k) for k in ("id", "status", "ops", "result", "error", "executor", "attempts")}
                for r in ksr.list_requests(conn, requester=tid, limit=10)]
    except Exception:  # health: allow BLE001 -- show must not fail on an unreadable side table
        logger.debug("kanban_show: supervisor requests unreadable", exc_info=True)
        return []
