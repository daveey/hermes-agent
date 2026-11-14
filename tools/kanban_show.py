"""Read-only card detail assembly for the Kanban tool facade."""
import json
from typing import Any


def fields(obj: Any, names: tuple[str, ...]) -> dict[str, Any]:
    """``{name: getattr(obj, name)}``; every value None when ``obj`` is None."""
    return {n: getattr(obj, n) if obj is not None else None for n in names}


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
    view = compact if compact_view(args) else (lambda out: out)
    with kt._granted_board(args.get("board"), tid) as (kb, conn, _):
        task = kb.get_task(conn, tid)
        if task is not None:
            return json.dumps(view(_detail(kb, conn, task, args, replica=False)))
        with kfc.open_replica(kfc._live_db_file(conn)) as replica:
            task = kb.get_task(replica, tid) if replica is not None else None
            if replica is not None and task is not None:
                owner = kfc.created_on(replica, tid)
                out = _detail(kb, replica, task, args, replica=True)
                out["owner"] = None if owner is kfc._UNSET else owner
                from hermes_cli.kanban_pickup_stall import latest as pickup_skip
                if skip := pickup_skip(conn, tid):  # why this host's pickup has not imported it [daveey]
                    out["pickup_skip"] = skip
                return json.dumps(view(out))
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
    details = {}
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
        details = kqp.detail_fields(conn, task, args.get("board"))
    from hermes_cli.kanban_hub_admission_placement import describe, latest_verdict
    from hermes_cli.kanban_wake_summary import wakes
    from hermes_cli.kanban_artifact_receipt import latest_receipt
    verdict = latest_verdict(conn, tid)
    if verdict and details.get("queue_note"):
        details["queue_note"] = f"{details['queue_note']} ({describe(verdict)})"
    return {
        "task": kt._fields(task, kt._TASK_FIELDS),
        "source": "replica" if replica else "live",
        **latest_receipt(kb.list_events(conn, tid)),
        **capsule,
        "queue_reason": kqr.latest(conn, tid),
        "wakes": wakes(conn, task),  # [daveey] {check, next_probe_at, backstop_at} while scheduled
        **details,
        **({"hub_admission": verdict} if verdict else {}),
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


# Kinds a worker never acts on one by one: a run of them collapses to its newest row plus a count. [daveey]
_REPEATED_EVENT_KINDS = frozenset({"heartbeat", "dispatch_skipped", "respawn_guarded", "wake_episode_evidence"})
_DIAGNOSTIC_FIELDS = ("capacity_evidence", "execution_observations")


def compact_view(args: dict) -> bool:
    """True when this read gets the compact payload: a dispatcher-owned worker that did not pass
    ``verbose=true``. Interactive and orchestrator callers keep the full shape."""
    from agent.delegation_context import owned_kanban_task
    from utils import is_truthy_value
    return not is_truthy_value(args.get("verbose"), default=False) and bool(owned_kanban_task())


def compact(out: dict) -> dict:
    """The worker-facing payload: capacity/observation diagnostics dropped (``capacity_note`` keeps
    the one-line reading), runs of repeated bookkeeping events collapsed, ``_``-prefixed internal
    payload keys stripped, and every long text ``worker_context`` already carries verbatim (the body,
    comment bodies, run summaries/errors/metadata) replaced by a pointer: a text the context caps or
    tail-drops stays. ``kanban_show(verbose=true)`` returns everything."""
    out = {k: v for k, v in out.items() if k not in _DIAGNOSTIC_FIELDS}
    context = out.get("worker_context") or ""
    if isinstance(out.get("task"), dict):
        out["task"] = _point(out["task"], ("body",), context)
    out["comments"] = [_point(c, ("body",), context) if isinstance(c, dict) else c
                       for c in out.get("comments") or []]
    out["runs"] = [_point(r, ("summary", "error", "metadata"), context) if isinstance(r, dict) else r
                   for r in out.get("runs") or []]
    out["events"] = collapse_events(out.get("events") or [])
    out["compact"] = ("diagnostics and repeated events omitted, texts shown in worker_context not repeated; "
                      "kanban_show(verbose=true) for the full payload")
    return out


_IN_CONTEXT = "(in worker_context)"
_MIN_POINTED_BYTES = 64  # shorter texts cost less than the pointer is worth


def _point(row: dict, keys: tuple[str, ...], context: str) -> dict:
    """``row`` with each of ``keys`` whose rendering appears verbatim in ``context`` replaced by a pointer."""
    out = dict(row)
    for key in keys:
        value = row.get(key)
        if key == "metadata":  # rendered as sorted JSON; a capped rendering never contains the whole dump
            text = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, dict) and value else ""
        else:
            text = value.strip() if isinstance(value, str) else ""
        if len(text.encode()) >= _MIN_POINTED_BYTES and text in context:
            out[key] = _IN_CONTEXT
    return out


def collapse_events(events: list[dict]) -> list[dict]:
    """Collapse consecutive same-kind, same-run rows of ``_REPEATED_EVENT_KINDS`` into the newest one
    with ``repeated`` (count) and ``first_at``; strip ``_``-prefixed payload keys from every row."""
    rows: list[dict] = []
    for event in events:
        payload = event.get("payload")
        if isinstance(payload, dict):
            event = {**event, "payload": {k: v for k, v in payload.items() if not str(k).startswith("_")}}
        prev = rows[-1] if rows else None
        if (prev is not None and event.get("kind") in _REPEATED_EVENT_KINDS
                and prev.get("kind") == event.get("kind") and prev.get("run_id") == event.get("run_id")):
            rows[-1] = {**event, "repeated": prev.get("repeated", 1) + 1,
                        "first_at": prev.get("first_at", prev.get("created_at"))}
            continue
        rows.append(event)
    return rows
