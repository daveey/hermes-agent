"""GET /board read, split for board_rows: ``materialize`` does every SQL read and nothing
else (retained until the database changes); ``project`` adds what can change without a
commit (clock, config, profiles on disk, hub replica) and runs on every refresh."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict
from typing import Callable, Optional

from hermes_cli import kanban_db
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_workflow
from plugins.kanban.dashboard.board_diagnostics import _attach_diagnostics

# Dashboard columns, left-to-right ("archived" is a filter toggle, not a column), from the
# one workflow definition. A status missing here gets mis-bucketed into ``todo``.
BOARD_COLUMNS: list[str] = list(kanban_workflow.DEFAULT_WORKFLOW.keys())

_CARD_SUMMARY_PREVIEW_CHARS = 200


def _placeholders(ids: list) -> str:
    return ",".join(["?"] * len(ids))


def fresh(value):
    """New dicts/lists all the way down (scalars shared). Projections hand these out so a
    caller editing a payload can never edit the retained rows."""
    if isinstance(value, dict):
        return {k: fresh(v) for k, v in value.items()}
    if isinstance(value, list):
        return [fresh(v) for v in value]
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

    def _rows_by_task(table: str) -> dict[str, tuple]:
        by_task: dict[str, list] = {tid: [] for tid in row_ids}
        for row in conn.execute(
            f"SELECT * FROM {table} WHERE task_id IN ({_placeholders(row_ids)}) ORDER BY id", tuple(row_ids)):
            by_task.setdefault(row["task_id"], []).append(row)
        return {tid: tuple(v) for tid, v in by_task.items()}  # sqlite3.Row is immutable

    events_by_task = _rows_by_task("task_events")
    runs_by_task = _rows_by_task("task_runs")
    graph_by_task = kanban_db.task_graph_contexts(conn, row_ids)
    from hermes_cli import kanban_wake_check as kwc
    wake_by_task = kwc.load_states(conn, row_ids)
    return tuple(rows), events_by_task, runs_by_task, graph_by_task, wake_by_task


def diagnostics_from_inputs(inputs: Optional[tuple]) -> dict[str, list[dict]]:
    """Run the rules over ``diagnostic_inputs``. Reads config, the clock and the hub replica
    now, so a retained input set still yields current diagnostics. Rules get fresh lists and
    copies of the graph/wake dicts, never the retained objects."""
    from hermes_cli.config import load_config

    if inputs is None:
        return {}
    rows, events_by_task, runs_by_task, graph_by_task, wake_by_task = inputs
    diag_config = kd.config_from_runtime_config(load_config())
    out: dict[str, list[dict]] = {}
    for r in rows:
        tid = r["id"]
        diags = kd.compute_task_diagnostics(
            r, list(events_by_task[tid]), list(runs_by_task[tid]), config=diag_config,
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


def materialize(conn, tenant, include_archived, workflow_template_id, current_step_key, done_limit, *,
                diagnostic_inputs: Callable = diagnostic_inputs) -> dict:
    """Everything the board payload reads from SQL, and nothing that depends on the clock,
    config, profiles on disk or the replica. Retained across refreshes: never mutate it."""
    tasks = kanban_db.list_tasks(
        conn, tenant=tenant, include_archived=include_archived,
        workflow_template_id=workflow_template_id, current_step_key=current_step_key)
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
    # Done cards past ``done_limit`` never reach the payload: trim them before the per-card
    # work (diagnostics alone were ~1.2 of the 1.5 s cold read on a 1.2k-card board).
    # Same order as the done column below: completed_at DESC NULLS LAST, id DESC.
    done_all = sorted(sorted((t for t in tasks if t.status == "done"), key=lambda t: t.id, reverse=True),
                      key=lambda t: (t.completed_at is None, -(t.completed_at or 0)))
    done_total = len(done_all)
    done_cap = int(done_limit or 0)
    dropped = {t.id for t in done_all[done_cap:]} if 0 < done_cap < done_total else set()
    tasks = [t for t in tasks if t.id not in dropped]
    diag_inputs = diagnostic_inputs(conn, task_ids=[t.id for t in tasks if t.status != "archived"])
    latest_event_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"]
    # One window-function query for latest summaries (avoids N+1); cards get a
    # truncated preview, the full text comes from /tasks/:id.
    summary_map = kanban_db.latest_summaries(conn, [t.id for t in tasks])
    # One query for the active run's start per card (avoids N+1); the run
    # clock ticks from this, not the task's first-ever start.
    run_start_map = kanban_db.current_run_started_ats(conn, [t.id for t in tasks])
    managers = managing_sessions(conn)
    cards = []
    for t in tasks:
        full = summary_map.get(t.id)
        cards.append((t, asdict(t), {
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
    return {
        "cards": tuple(cards), "diag_inputs": diag_inputs, "done_total": done_total,
        "latest_event_id": int(latest_event_id), "tenants": tuple(tenants),
        # The SQL half of kanban_db.known_assignees; profiles on disk are added per projection.
        "task_assignees": frozenset(kanban_db._counts_by_assignee(conn)),
    }


def project(rows: dict, include_archived, diagnostics_from_inputs: Callable = diagnostics_from_inputs) -> dict:
    """The /board payload from ``materialize`` rows plus what may change without a
    commit (clock, config, profiles on disk, replica). Builds new containers throughout."""
    diagnostics_per_task = diagnostics_from_inputs(rows["diag_inputs"])
    columns: dict[str, list[dict]] = {c: [] for c in BOARD_COLUMNS}
    if include_archived:
        columns["archived"] = []
    for t, fields, extra in rows["cards"]:
        d = fresh(fields)
        # Derived age metrics so the UI can colour stale cards without client deltas.
        try:
            d["age"] = kanban_db.task_age(t)
        except Exception:  # health: allow BLE001 -- same fallback as _task_dict: one bad row must not 500 the board
            d["age"] = {"created_age_seconds": None, "started_age_seconds": None, "time_to_complete_seconds": None}
        d.update(fresh(extra))
        _attach_diagnostics(d, diagnostics_per_task.get(t.id))
        preview_list_body(d)
        columns[t.status if t.status in columns else "todo"].append(d)

    # Queue lanes keep the list_tasks dispatch order; the done column is
    # history, so order it newest-completed-first. Two stable sorts compose
    # into the "completed_at DESC NULLS LAST, id DESC" SQL key.
    columns["done"].sort(key=lambda d: d["id"], reverse=True)
    columns["done"].sort(key=lambda d: (d["completed_at"] is None, -(d["completed_at"] or 0)))

    # Lane-by-profile sub-grouping: the same names as kanban_db.known_assignees (and the
    # /assignees picker): profiles holding non-archived tasks plus profiles configured on
    # disk, so a freshly-added profile shows up as a (possibly empty) lane immediately.
    assignees = sorted(set(kanban_db.list_profiles_on_disk()) | rows["task_assignees"])
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
        "assignees": assignees, "latest_event_id": rows["latest_event_id"], "now": int(time.time())}
