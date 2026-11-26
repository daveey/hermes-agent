"""GET /board read, split for board_rows: ``materialize`` does every SQL read and nothing
else (retained until the database changes); ``project`` adds what can change without a
commit (clock, config, profiles on disk, hub replica) and runs on every refresh."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict
from types import MappingProxyType
from typing import Callable, Optional

from hermes_cli import kanban_db
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_workflow
from plugins.kanban.dashboard.board_diagnostics import _attach_diagnostics
from plugins.kanban.dashboard.board_run_history import retained_runs
from plugins.kanban.dashboard.board_event_history import extend as extend_histories
from plugins.kanban.dashboard.board_event_history import same_histories as _same_event_histories

# Dashboard columns, left-to-right ("archived" is a filter toggle, not a column), from the
# one workflow definition. A status missing here gets mis-bucketed into ``todo``.
BOARD_COLUMNS: list[str] = list(kanban_workflow.DEFAULT_WORKFLOW.keys())

_CARD_SUMMARY_PREVIEW_CHARS = 200


def _placeholders(ids: list) -> str:
    return ",".join(["?"] * len(ids))


def _rows_by_task(conn: sqlite3.Connection, table: str, row_ids: list) -> dict[str, tuple]:
    by_task: dict[str, list] = {tid: [] for tid in row_ids}
    if row_ids:
        # Only per-task arrival order is consumed. Sorting SELECT * in SQLite
        # builds a temporary B-tree containing every payload/metadata byte, often
        # spilling to disk on a cold board. Sort row references after grouping
        # instead; the task index need not have arrival-id order (backfills can
        # have older timestamps). No history or scalar columns are discarded.
        for row in conn.execute(
                f"SELECT * FROM {table} WHERE task_id IN ({_placeholders(row_ids)})", tuple(row_ids)):
            by_task.setdefault(row["task_id"], []).append(row)
        for rows in by_task.values():
            rows.sort(key=lambda row: row["id"])
    return {tid: (kd.EventHistory(v) if table == "task_events"
                  else tuple(MappingProxyType(dict(row)) for row in v)) for tid, v in by_task.items()}


def _raw(rows) -> tuple:
    return tuple(tuple(r.values()) if isinstance(r, MappingProxyType) else tuple(r) for r in rows)


_NO_EVENTS = (0, None)  # _event_marks of a task without events


def _wake_states(conn: sqlite3.Connection, row_ids: list) -> dict:
    from hermes_cli import kanban_wake_check as kwc  # lazy, as before the incremental split
    return kwc.load_states(conn, row_ids)


def _event_marks(conn: sqlite3.Connection, row_ids: list) -> dict[str, tuple]:
    """Append/delete marks, read in the materializer's snapshot.

    Same-ID edits/replacements additionally invalidate through P2; these marks
    also detect heartbeat inserts/deletes excluded from that content cursor.
    """
    if not row_ids:
        return {}
    # COUNT uses the covering task-id index; MAX uses a single last-row seek.
    # A grouped COUNT/MAX executes both aggregates for every historical event.
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        f"WITH wanted(task_id) AS (VALUES {','.join(['(?)'] * len(row_ids))}) "
        "SELECT task_id, (SELECT COUNT(*) FROM task_events e WHERE e.task_id=w.task_id), "
        "(SELECT MAX(id) FROM task_events e WHERE e.task_id=w.task_id) FROM wanted w",
        tuple(row_ids)) if r[1]}


def _unmarked_event_marks(conn: sqlite3.Connection, row_ids: list) -> dict[str, tuple]:
    """``{task_id: (count, max id)}`` of only the event kinds the P2 cursor does not mark.

    P2 marks the task of every other event insert and of every event update or delete
    (``kanban_activity_revision``), so under a trusted cursor these inserts are the only
    event changes left to detect. COUNT catches an insert at any id (pickup history
    backfills below every id); both aggregates seek ``idx_events_kind_task_id`` and never
    touch the rest of the history."""
    from hermes_cli.kanban_activity_revision import UNREAD_EVENT_KINDS
    if not row_ids:
        return {}
    kinds = ",".join(["?"] * len(UNREAD_EVENT_KINDS))
    # Correlated aggregates: one range seek per task on (kind, task_id, id). A LEFT JOIN +
    # GROUP BY over the same index cost about 4x as much (same counts and maxima).
    match = f"FROM task_events e WHERE e.kind IN ({kinds}) AND e.task_id = w.task_id"
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        f"WITH wanted(task_id) AS (VALUES {','.join(['(?)'] * len(row_ids))}) "
        f"SELECT w.task_id, (SELECT COUNT(*) {match}), (SELECT MAX(e.id) {match}) FROM wanted w",
        (*row_ids, *UNREAD_EVENT_KINDS, *UNREAD_EVENT_KINDS))}


def fresh(value):
    """New dicts/lists all the way down (scalars shared). Projections hand these out so a
    caller editing a payload can never edit the retained rows."""
    if isinstance(value, dict):
        return {k: fresh(v) if isinstance(v, (dict, list)) else v for k, v in value.items()}
    if isinstance(value, list):
        return [fresh(v) if isinstance(v, (dict, list)) else v for v in value]
    return value


def diagnostic_inputs(conn: sqlite3.Connection, task_ids: Optional[list[str]] = None) -> Optional[tuple]:
    """The rows the diagnostics rules read: ``(task rows, events by task, runs by task, graph
    contexts, wake-check states)`` via aggregate queries; None when there are no tasks. SQL only:
    the board retains these between commits (board_rows) and re-runs the rules on each refresh."""
    if task_ids is not None and not task_ids:
        return None
    if task_ids is not None:
        rows = conn.execute(f"SELECT * FROM tasks WHERE id IN ({_placeholders(task_ids)})", tuple(task_ids)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks WHERE status != 'archived'").fetchall()
    if not rows:
        return None
    row_ids = [r["id"] for r in rows]
    events_by_task = _rows_by_task(conn, "task_events", row_ids)
    runs_by_task = _rows_by_task(conn, "task_runs", row_ids)
    graph_by_task = kanban_db.task_graph_contexts(conn, row_ids)
    wake_by_task = _wake_states(conn, row_ids)
    return tuple(rows), events_by_task, runs_by_task, graph_by_task, wake_by_task


def diagnostics_from_inputs(inputs: Optional[tuple]) -> dict[str, list[dict]]:
    """Run the rules over ``diagnostic_inputs``. Reads config, the clock and the hub replica
    now, so a retained input set still yields current diagnostics. Event histories are immutable;
    rules get fresh run lists and copies of graph/wake dicts."""
    from hermes_cli.config import load_config

    if inputs is None:
        return {}
    rows, events_by_task, runs_by_task, graph_by_task, wake_by_task = inputs
    diag_config = kd.config_from_runtime_config(load_config())
    out: dict[str, list[dict]] = {}
    for r in rows:
        tid = r["id"]
        diags = kd.compute_task_diagnostics(
            dict(zip(r.keys(), r)) if isinstance(r, sqlite3.Row) else r,
            events_by_task[tid], list(runs_by_task[tid]), config=diag_config,
            graph=fresh(graph_by_task.get(tid)), wake_check=fresh(wake_by_task.get(tid)))
        if diags:
            out[tid] = [d.to_dict() for d in diags]
    return out


def managing_sessions(conn: sqlite3.Connection) -> dict[str, str]:
    """``{task_id: chat session id}`` of each card's most recent ``tui`` notify
    subscriber — a session asked to shepherd someone else's card subscribes to
    it, so the latest subscriber is the one managing it now. [daveey]"""
    try:
        rows = conn.execute(
            "SELECT task_id, chat_id FROM kanban_notify_subs "
            "WHERE platform = 'tui' AND chat_id != '' ORDER BY created_at, rowid").fetchall()
    except sqlite3.Error:  # board predates notify subscriptions
        return {}
    return {str(r[0]): str(r[1]) for r in rows}


def preview_list_body(task: dict) -> None:
    """List cards carry a short body. The drawer loads the full text from /tasks/:id."""
    body = task.get("body")
    if isinstance(body, str) and len(body) > _CARD_SUMMARY_PREVIEW_CHARS:
        task["body"] = body[:_CARD_SUMMARY_PREVIEW_CHARS]
        task["body_truncated"] = True


def _done_trim(conn, filters: dict, done_limit) -> tuple[str, list, int]:
    return _done_state(conn, filters, done_limit)[:3]


def _done_state(conn, filters: dict, done_limit) -> tuple[str, list, int, frozenset, tuple]:
    """``(extra_where, params, done total)`` for list_tasks. Done cards past ``done_limit`` never reach
    the payload, so they are ranked on a narrow read and never fetched whole (4.2k of the hub's 4.4k
    rows: parsing them was half of every board read). Same order as the done column below:
    completed_at DESC NULLS LAST, id DESC, on the values Task.from_row parses."""
    # This prepass re-ranks by normalized completion below; dispatcher priority
    # order only adds recursive dependency walks over the entire done history.
    done = kanban_db.list_task_rows(conn, status="done", columns="rowid AS _rowid, id, completed_at",
                                   order_by="completed-desc", **filters)
    done_cap = int(done_limit or 0)
    done_ids = frozenset(kanban_db._lossy_text(r["id"]) for r in done)
    if not 0 < done_cap < len(done):
        window = ()
        if done_cap > 0:  # the delta path's ranked window: every done card, newest first
            from plugins.kanban.dashboard.board_incremental import rank_key
            window = tuple(sorted(((kanban_db._lossy_text(r["id"]), kanban_db._to_epoch(
                kanban_db._lossy_text(r["completed_at"]))) for r in done), key=rank_key))
        return "", [], len(done), done_ids, window
    epoch = {r["_rowid"]: kanban_db._to_epoch(kanban_db._lossy_text(r["completed_at"])) for r in done}
    ranked = sorted(sorted(done, key=lambda r: kanban_db._lossy_text(r["id"]), reverse=True),
                    key=lambda r: (epoch[r["_rowid"]] is None, -(epoch[r["_rowid"]] or 0)))
    keep = [r["_rowid"] for r in ranked[:done_cap]]
    window = tuple((kanban_db._lossy_text(r["id"]), epoch[r["_rowid"]]) for r in ranked[:done_cap])
    return f"status != 'done' OR rowid IN ({_placeholders(keep)})", keep, len(done), done_ids, window


def _history_mark(events) -> Optional[tuple]:
    """``_event_marks`` of exactly these retained events (rows come ``ORDER BY id``):
    whatever the database says next is compared with what was actually read."""
    if not events:
        return _NO_EVENTS
    last = events[-1]
    try:
        return (len(events), last["id"])
    except (KeyError, IndexError, TypeError):
        return None  # cannot vouch: the next refresh reloads


def _diag_memo(rows, events_by_task, runs_by_task, marks, unmarked=None) -> dict:
    """Per task: what its diagnostics inputs were read from, to tell a changed task next time:
    ``(task row, raw runs or None, event count/max, events, runs, unmarked-kind count/max)``."""
    return {r["id"]: (tuple(r), _raw(runs_by_task.get(r["id"], ())), marks.get(r["id"], _NO_EVENTS),
                      events_by_task.get(r["id"], ()), runs_by_task.get(r["id"], ()),
                      None if unmarked is None else unmarked.get(r["id"])) for r in rows}


def _same_events(conn, tid, history) -> bool:
    """Scalar compatibility seam for existing callers and pre-batch parity tests."""
    from plugins.kanban.dashboard.board_event_history import same_events
    return same_events(conn, tid, history)


def _incremental_diag_inputs(conn, diag_rows: list, prev: dict, diagnostic_inputs: Callable, *,
                             graphs=None, event_changed=None) -> tuple:
    """Retain event histories independently of task/run bookkeeping. ``event_changed``
    is the trusted P2 invalidation set: it marks every event insert, update and delete
    except inserts of ``UNREAD_EVENT_KINDS`` (heartbeats), which ``_unmarked_event_marks``
    catches with index seeks. Marked tasks compare exact history only when count/max
    cannot already prove a change; unmarked tasks retain histories without scanning.
    Legacy callers without a cursor compare whole-history count/max plus the task row and
    runs (conservative). Clock/config rules stay fresh.

    A history read in the caller's transaction is retained with its seal (memo slot 6,
    ``board_event_history.extend``). A moved task with a seal is compared in SQL instead:
    one row per task proves all but its newest ``RECENT`` rows unchanged, and only those and
    any new rows are read again (a 9.3k-event hub card gaining a skip event every few seconds
    re-read all 9.3k rows, twice when its marks were equal).
    """
    ids = [r["id"] for r in diag_rows]
    trusted = event_changed is not None
    unmarked = _unmarked_event_marks(conn, ids)
    if trusted:  # memos built without unmarked marks fall back to whole-history marks
        marks = _event_marks(conn, [tid for tid in ids if tid in prev
                                    and (tid in event_changed or len(prev[tid]) < 6 or prev[tid][5] is None)])
    else:
        marks = _event_marks(conn, ids)
    runs = retained_runs(conn, ids, prev, event_changed, _rows_by_task)
    seals = {tid: prev[tid][6] for tid in ids if tid in prev and len(prev[tid]) > 6 and prev[tid][6] is not None}
    same_events = _same_event_histories(conn, {  # a sealed history is compared by extend() below
        tid: prev[tid][3] for tid in ids if trusted and tid in event_changed and tid not in seals
        and tid in prev and prev[tid][2] == marks.get(tid, _NO_EVENTS)
    })

    def moved(r, tid) -> bool:
        old = prev.get(tid)
        if old is None:
            return True
        if trusted:
            if tid in event_changed:
                # An append/delete is already known to differ: avoid scanning it
                # once for equality and again to reload. Equal marks still need
                # the exact comparison for same-ID edits and replacements.
                if old[2] != marks.get(tid, _NO_EVENTS) or tid in seals:
                    return True
                return not same_events[tid]
            if len(old) > 5 and old[5] is not None:
                return old[5] != unmarked.get(tid)
            return old[2] is None or old[2] != marks.get(tid, _NO_EVENTS)
        if old[2] is None or old[2] != marks.get(tid, _NO_EVENTS) or old[0] != tuple(r):
            return True
        return (old[1] if old[1] is not None else _raw(old[4])) != _raw(runs[tid])

    changed = {tid for r, tid in zip(diag_rows, ids) if moved(r, tid)}
    # A moved task whose sealed head is unchanged keeps it and re-reads only its newest rows; the rest reload.
    # A history just proven equal (unsealed, marked, same marks) is sealed too, so the next
    # mark of that task costs one row instead of another exact rescan.
    resealed = {tid for tid, same in same_events.items() if same and tid not in changed}
    grown, new_seals = extend_histories(conn, {
        tid: (prev[tid][3] if tid in prev else None, seals.get(tid)) for tid in ids
        if tid in changed or tid in resealed})
    reload = [tid for tid in ids if tid in changed and tid not in grown]
    part = diagnostic_inputs(conn, task_ids=reload) if reload else None
    fresh_events = {**(part[1] if part is not None else {}), **grown}
    events = {}
    memo = {}
    for r, tid in zip(diag_rows, ids):
        if tid in fresh_events or tid not in changed:
            events[tid] = fresh_events[tid] if tid in fresh_events else prev[tid][3]
            # Raw run signatures are only compared by a legacy refresh, which derives them then.
            memo[tid] = (tuple(r), None, _history_mark(events[tid]), events[tid], runs[tid], unmarked.get(tid),
                         new_seals.get(tid) if tid in changed or tid in resealed else seals.get(tid))
        else:  # vanished between reads: no events held, so never vouch for a mark
            events[tid] = ()
            memo[tid] = (tuple(r), None, None, (), runs[tid], None)
    if graphs is None:
        graphs = kanban_db.task_graph_contexts(conn, ids)
    inputs = (tuple(diag_rows), events, runs, graphs, _wake_states(conn, ids))
    return inputs, memo


def materialize(conn, tenant, include_archived, workflow_template_id, current_step_key, done_limit, *,
                diagnostic_inputs: Callable = diagnostic_inputs, prev: Optional[dict] = None,
                changed=None, selected_ids=None) -> dict:
    """Everything the board payload reads from SQL, and nothing that depends on the clock,
    config, profiles on disk or the replica. Retained across refreshes: never mutate it.

    ``prev`` (the last rows for the same query) makes it incremental: a task whose row values are
    unchanged keeps its parsed Task, and only tasks whose row, runs or events moved re-read their
    events. Every displayed row is still read, so unmarked bookkeeping writes are never missed.
    ``prev`` with ``changed`` (a frozenset of task ids from ``kanban_task_change.changed_since``, taken in the
    same snapshot as this read) takes ``board_incremental.delta``: displayed IDs union marked IDs are
    read by primary key, with bounded done history. ``changed`` omitted, ``FULL`` or a set the delta cannot vouch for reads in
    full, exactly as before. ``selected_ids`` is a shadow board's externally ranked,
    collision-free display set; it bypasses the single-file window/delta, not filters.
    """
    filters = dict(tenant=tenant, include_archived=include_archived,
                   workflow_template_id=workflow_template_id, current_step_key=current_step_key)
    if selected_ids is None and prev is not None and isinstance(changed, (set, frozenset)):
        from plugins.kanban.dashboard import board_incremental
        rows = board_incremental.delta(conn, filters, done_limit, diagnostic_inputs, prev, changed)
        if rows is not None:
            return rows
    if selected_ids is None:
        extra_where, extra_params, done_total, done_ids, done_window = _done_state(conn, filters, done_limit)
    else:
        extra_where = f"id IN ({_placeholders(selected_ids)})" if selected_ids else "0"
        extra_params = list(selected_ids)
        done_total, done_ids, done_window = 0, frozenset(), ()
    old = (prev or {}).get("_memo")
    old_tasks = old["tasks"] if old is not None else {}
    parsed: dict[tuple, tuple] = {}
    task_rows, fields, raw_keys = [], [], []

    def parse(r):
        raw = (tuple(r.keys()), tuple(r))  # column names too: a migration re-parses every row
        hit = parsed.get(raw) or old_tasks.get(raw)
        if hit is None:
            t = kanban_db.Task.from_row(r)
            hit = (t, asdict(t))
        parsed[raw] = hit
        task_rows.append(r)
        fields.append(hit[1])
        raw_keys.append(raw)
        return hit[0]

    tasks = kanban_db.list_tasks(conn, extra_where=extra_where, extra_params=extra_params, parse=parse, **filters)
    if len(task_rows) != len(tasks):  # a list_tasks that bypassed ``parse``: nothing to reuse
        task_rows, fields, raw_keys, parsed, old = [], [asdict(t) for t in tasks], [], {}, None
    # Link / comment / progress rollups are each one aggregate query rather than N per-task lookups.
    link_counts: dict[str, dict[str, int]] = {}
    for row in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall():
        link_counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})["children"] += 1
        link_counts.setdefault(row["child_id"], {"parents": 0, "children": 0})["parents"] += 1
    comment_counts: dict[str, int] = {
        r["task_id"]: r["n"] for r in conn.execute("SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id")}
    progress: dict[str, dict[str, int]] = {}  # per parent: children done / total, rendered as "N/M"
    for row in conn.execute(
        "SELECT l.parent_id AS pid, t.status AS cstatus FROM task_links l JOIN tasks t ON t.id = l.child_id").fetchall():
        p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
        p["total"] += 1
        p["done"] += row["cstatus"] == "done"
    diag_ids = [t.id for t in tasks if t.status != "archived"]
    if old is not None and diag_ids:
        by_id = {r["id"]: r for r in task_rows}
        diag_rows = [by_id[tid] for tid in diag_ids if tid in by_id]
        diag_inputs, diag_memo = _incremental_diag_inputs(
            conn, diag_rows, old["diag"], diagnostic_inputs,
            event_changed=changed if selected_ids is not None and isinstance(changed, (set, frozenset)) else None)
    else:
        marks = _event_marks(conn, diag_ids)  # before the events it vouches for
        unmarked = _unmarked_event_marks(conn, diag_ids)
        diag_inputs = diagnostic_inputs(conn, task_ids=diag_ids)
        diag_memo = (_diag_memo(diag_inputs[0], diag_inputs[1], diag_inputs[2], marks, unmarked)
                     if diag_inputs is not None else {})
    latest_event_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"]
    # One window-function query for latest summaries (avoids N+1); cards get a
    # truncated preview, the full text comes from /tasks/:id.
    summary_map = kanban_db.latest_summaries(conn, [t.id for t in tasks])
    # One query for the active run's start per card (avoids N+1); the run
    # clock ticks from this, not the task's first-ever start.
    run_start_map = kanban_db.current_run_started_ats(conn, [t.id for t in tasks])
    managers = managing_sessions(conn)
    cards = []
    for t, task_fields in zip(tasks, fields):
        full = summary_map.get(t.id)
        cards.append((t, task_fields, {
            "latest_summary": full[:_CARD_SUMMARY_PREVIEW_CHARS] if full else None,
            "current_run_started_at": run_start_map.get(t.id),
            "link_counts": link_counts.get(t.id, {"parents": 0, "children": 0}),
            "comment_count": comment_counts.get(t.id, 0),
            "progress": progress.get(t.id),  # None when the task has no children
            # [daveey] The chat shepherding the card: its latest ``tui`` notify
            # subscriber, else the session that created it.
            "managing_session_id": managers.get(t.id) or (t.session_id or None),
        }))
    tenants = [r["tenant"] for r in conn.execute("SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant")]
    from plugins.kanban.dashboard.board_incremental import filters_key
    inc = None
    if selected_ids is not None:
        done_total = sum(t.status == "done" for t in tasks)
    if selected_ids is None and raw_keys and int(done_limit or 0) > 0:  # a later single-file delta
        inc = {"filters": filters_key(filters), "cap": int(done_limit), "done_ids": done_ids, "window": done_window,
               "raw": {t.id: k for t, k in zip(tasks, raw_keys)}}
    return {
        "cards": tuple(cards), "diag_inputs": diag_inputs, "done_total": done_total,
        "latest_event_id": int(latest_event_id), "tenants": tuple(tenants),
        # The SQL half of kanban_db.known_assignees; profiles on disk are added per projection.
        "task_assignees": frozenset(kanban_db._counts_by_assignee(conn)),
        # What the next read of the same query diffs against (``prev``); never part of the payload.
        "_memo": {"tasks": parsed, "diag": diag_memo, "inc": inc},
    }


def project(rows: dict, include_archived, diagnostics_from_inputs: Callable = diagnostics_from_inputs) -> dict:
    """The /board payload from ``materialize`` rows plus what may change without a
    commit (clock, config, profiles on disk, replica). Builds new containers throughout."""
    diagnostics_per_task = diagnostics_from_inputs(rows["diag_inputs"])
    ages = {}
    for task, _fields, _extra in rows["cards"]:
        try:
            ages[task.id] = kanban_db.task_age(task)
        except Exception:  # health: allow BLE001 -- preserve one-bad-row age fallback
            ages[task.id] = {"created_age_seconds": None, "started_age_seconds": None, "time_to_complete_seconds": None}
    assignees = sorted(set(kanban_db.list_profiles_on_disk()) | rows["task_assignees"])
    return project_captured(rows, include_archived, diagnostics_per_task=diagnostics_per_task,
                            ages=ages, assignees=assignees, now=int(time.time()))


def project_captured(rows: dict, include_archived: bool, *, diagnostics_per_task: dict,
                     ages: dict, assignees: list, now: int) -> dict:
    """Pure canonical board projection, shared by legacy and owner reducers.

    Inputs are a coherent captured materialization plus explicit diagnostic, age,
    profile-roster and clock projections. This function never resolves a source;
    callers must not substitute empty mappings for unavailable source inputs.
    Shadow merging remains a separate canonical projection step.
    """
    plain = {**rows, "cards": tuple((fields, extra) for _t, fields, extra in rows["cards"])}
    return project_fields(plain, include_archived, diagnostics_per_task=diagnostics_per_task,
                          ages=ages, assignees=assignees, now=now)


def project_fields(rows, include_archived, *, diagnostics_per_task, ages, assignees, now):
    """Canonical serializer over detached plain fields; no retained Task objects."""
    columns: dict[str, list[dict]] = {c: [] for c in BOARD_COLUMNS}
    if include_archived:
        columns["archived"] = []
    for fields, extra in rows["cards"]:
        d = fresh(fields)
        d["age"] = fresh(ages[fields["id"]])
        d.update(fresh(extra))
        _attach_diagnostics(d, fresh(diagnostics_per_task.get(fields["id"])))
        preview_list_body(d)
        status = fields["status"]
        columns[status if status in columns else "todo"].append(d)

    # Queue lanes keep the list_tasks dispatch order; the done column is
    # history, so order it newest-completed-first. Two stable sorts compose
    # into the "completed_at DESC NULLS LAST, id DESC" SQL key.
    columns["done"].sort(key=lambda d: d["id"], reverse=True)
    columns["done"].sort(key=lambda d: (d["completed_at"] is None, -(d["completed_at"] or 0)))

    # The caller supplies the canonical profile/task-assignee union and ordering.
    done_total = rows["done_total"]
    columns_out = []
    for name, tasks in columns.items():
        col = {"name": name, "tasks": tasks}
        if name == "done":
            col["total"] = done_total
            col["omitted"] = done_total - len(tasks)
        columns_out.append(col)
    return {
        "columns": columns_out, "tenants": list(rows["tenants"]),
        "assignees": list(assignees), "latest_event_id": rows["latest_event_id"], "now": int(now)}
