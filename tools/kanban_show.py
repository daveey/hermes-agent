"""Read-only card detail assembly for the Kanban tool facade."""
import json


def show(args: dict) -> str:
    from hermes_cli import kanban_queue_reason as kqr
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
    with kt._granted_board(args.get("board"), tid) as (kb, conn, _):
        task = kt._existing_task(kb, conn, tid)
        from tools.kanban_tools_capsule import own_card_view
        worker_context, capsule = own_card_view(task, tid, kb.build_worker_context(conn, tid))
        # A card ready and unclaimed 30+ min gets its queue place and the moves that start it (kanban-447c98343f):
        # "ready but has not started" was reported as a status instead of acted on. [daveey]
        from hermes_cli.kanban_parent_unclaimed import ready_hint
        try:
            queue_hint = ready_hint(conn, task)
        except Exception:  # health: allow BLE001 -- an advisory hint must never fail the read
            queue_hint = None
        # Every ready card says where it waits, same dict as the REST read (kanban-7d720d4ab8): a null queue_reason
        # for a card waiting on a full hub read as stuck, and a chat ran its work outside the dispatcher. [daveey]
        from hermes_cli import kanban_queue_position as kqp
        queue = kqp.read(conn, task, args.get("board"))
        return json.dumps({
            "task": kt._fields(task, kt._TASK_FIELDS),
            **capsule,
            "queue_reason": kqr.latest(conn, tid),
            **kqp.fields(queue),
            **({"queue_hint": queue_hint} if queue_hint else {}),
            "parents": kb.parent_ids(conn, tid),
            "unsatisfied_parents": [
                {"id": pid, "status": status} for pid, status in kb.unsatisfied_parents(conn, tid)],
            "children": kb.child_ids(conn, tid),
            "comments": [kt._fields(c, kt._COMMENT_FIELDS) for c in kb.list_comments(conn, tid)],
            "undelivered_comments": [c.id for c in kb.undelivered_live_comments(conn, tid)],
            "events": [kt._fields(e, kt._EVENT_FIELDS) for e in kb.list_events(conn, tid)[-50:]],
            "runs": [kt._fields(r, kt._RUN_FIELDS) for r in kb.list_runs(conn, tid)],
            "supervisor_requests": kt._graph.supervisor_requests(conn, tid),
            "worker_context": worker_context})
