"""Read-only card detail assembly for the Kanban tool facade."""
import json


def show(args: dict) -> str:
    from hermes_cli import kanban_foreign_card as kfc
    from tools import kanban_tools as kt

    tid = kt._default_task_id(args.get("task_id"))
    if not tid:
        return json.dumps({
            "current_task": None,
            "hint": (
                "No dispatcher-assigned task in this session, so there is no "
                "task to show by default. Call kanban_list to see task ids on "
                "the board, then kanban_show(task_id=<id>) for full state."),
        })
    # Retain board grants before consulting a replica; a foreign read is not a
    # cross-board permission grant, and never imports a writable local card.
    with kt._granted_board(args.get("board"), tid) as (kb, conn, _):
        task = kb.get_task(conn, tid)
        if task is not None:
            return json.dumps(_detail(kb, conn, task, args, replica=False))
        with kfc.open_replica(kfc._live_db_file(conn)) as replica:
            task = kb.get_task(replica, tid) if replica is not None else None
            if replica is not None and task is not None:
                owner = kfc.created_on(replica, tid)
                out = _detail(kb, replica, task, args, replica=True)
                out["owner"] = None if owner is kfc._UNSET else owner
                return json.dumps(out)
        kt._existing_task(kb, conn, tid)  # preserve the existing not-found error
    raise AssertionError("missing task lookup must reject")


def _detail(kb, conn, task, args, *, replica: bool) -> dict:
    from hermes_cli import kanban_queue_reason as kqr
    from hermes_cli import kanban_queue_position as kqp
    from tools import kanban_tools as kt
    from tools.kanban_tools_capsule import own_card_view

    tid = task.id
    worker_context = kb.build_worker_context(conn, tid)
    capsule = {}
    queue = None
    queue_hint = None
    if not replica:
        worker_context, capsule = own_card_view(task, tid, worker_context)
        # Queue position and capsule identity belong to this gateway's live
        # dispatcher, not to a replicated owner's cached task/run IDs.
        from hermes_cli.kanban_parent_unclaimed import ready_hint
        try:
            queue_hint = ready_hint(conn, task)
        except Exception:  # health: allow BLE001 -- an advisory hint must never fail the read
            queue_hint = None
        queue = kqp.read(conn, task, args.get("board"))
    return {
        "task": kt._fields(task, kt._TASK_FIELDS),
        "source": "replica" if replica else "live",
        **capsule,
        "queue_reason": kqr.latest(conn, tid),
        **kqp.fields(queue),
        **({"queue_hint": queue_hint} if queue_hint else {}),
        "parents": kb.parent_ids(conn, tid),
        "unsatisfied_parents": [
            {"id": pid, "status": status} for pid, status in kb.unsatisfied_parents(conn, tid)],
        "children": kb.child_ids(conn, tid),
        "comments": [kt._fields(c, kt._COMMENT_FIELDS) for c in kb.list_comments(conn, tid)],
        "undelivered_comments": [] if replica else [c.id for c in kb.undelivered_live_comments(conn, tid)],
        "events": [kt._fields(e, kt._EVENT_FIELDS) for e in kb.list_events(conn, tid)[-50:]],
        "runs": [kt._fields(r, kt._RUN_FIELDS) for r in kb.list_runs(conn, tid)],
        "supervisor_requests": kt._graph.supervisor_requests(conn, tid),
        "worker_context": worker_context}
