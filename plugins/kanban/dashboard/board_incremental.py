"""Exact delta refresh of board rows (P2). [daveey]

Read displayed task IDs union marked IDs by primary key on every refresh: bookkeeping
can change on *any* lane without an activity mark. Only changed raw tuples are parsed.
Diagnostics check event marks/runs for every displayed row, retaining unchanged events.
Hidden done history stays a narrow ranked window; uncertainty falls back to a full read.
The caller holds one read snapshot around the change cursor and this materialization.
"""

from __future__ import annotations

import functools
import marshal
import sqlite3
from dataclasses import asdict
from typing import Callable, Optional

from hermes_cli import kanban_db

MAX_CHANGED = 400


def _ph(ids) -> str:
    return ",".join(["?"] * len(ids))


def filters_key(filters: dict) -> tuple:
    return tuple(sorted((k, filters[k]) for k in filters))


def _cmp(a: tuple, b: tuple) -> int:
    """completed_at DESC NULLS LAST, id DESC, on parsed epoch values."""
    ka, kb = (a[1] is None, -(a[1] or 0)), (b[1] is None, -(b[1] or 0))
    if ka != kb:
        return -1 if ka < kb else 1
    return -1 if a[0] > b[0] else (1 if a[0] < b[0] else 0)


rank_key = functools.cmp_to_key(_cmp)


def done_epoch(row) -> Optional[int]:
    return kanban_db._to_epoch(kanban_db._lossy_text(row["completed_at"]))


def _fetch(conn, filters: dict, ids: list) -> dict:
    if not ids:
        return {}
    # Keyed by ID, so any cheap order will do: the default dispatch order evaluates a
    # recursive effective-priority subquery per row (most of this read's cost).
    rows = kanban_db.list_task_rows(conn, extra_where=f"id IN ({_ph(ids)})", extra_params=tuple(ids),
                                    order_by="created", **filters)
    return {kanban_db._lossy_text(r["id"]): r for r in rows}


def _bookkeeping_overlay(conn, filters: dict, pcards: dict, changed: list, retained: Optional[dict]) -> dict:
    """``{task id: current row}`` for displayed and marked IDs, re-reading whole rows only when needed.

    Under a trusted P2 cursor an unmarked task can only have moved in its
    ``BOOKKEEPING_COLUMNS`` (claim, lease, heartbeat and sync clocks): every other column write
    marks it. So one narrow indexed read of those columns, compared typed (``marshal``: 1, 1.0
    and True differ) with the retained row, decides which unmarked rows are still exact; marked,
    moved or missing rows are read in full. Without retained rows (the first delta after a full
    read built by an older materializer) every row is read in full, as before.
    """
    from hermes_cli.kanban_activity_revision import BOOKKEEPING_COLUMNS

    changed_set = set(changed)
    want = sorted(changed_set | set(pcards))
    keep = [tid for tid in pcards if tid not in changed_set]
    if retained is None or not keep or any(tid not in retained for tid in keep):
        return _fetch(conn, filters, want)
    keys = tuple(retained[keep[0]].keys())
    cols = [c for c in keys if c in BOOKKEEPING_COLUMNS["tasks"]]
    if any(tuple(retained[tid].keys()) != keys for tid in keep):
        return _fetch(conn, filters, want)
    at = [keys.index(c) for c in cols]
    narrow = {kanban_db._lossy_text(r[0]): tuple(r)[1:] for r in kanban_db.list_task_rows(
        conn, columns=", ".join(("id", *cols)), extra_where=f"id IN ({_ph(keep)})", extra_params=tuple(keep),
        order_by="created", **filters)}
    by_id, reread = {}, set(changed)
    for tid in keep:
        row = retained[tid]
        now = narrow.get(tid)
        if now is not None and marshal.dumps(now, 2) == marshal.dumps(tuple(row[i] for i in at), 2):
            by_id[tid] = row
        else:
            reread.add(tid)
    by_id.update(_fetch(conn, filters, sorted(reread)))
    return by_id


def _rollups(conn, ids) -> dict[str, dict]:
    """Content-only aggregates for invalidated displayed tasks."""
    ids = list(ids)
    if not ids:
        return {}
    ph, params = _ph(ids), tuple(ids)
    counts = {tid: {"parents": 0, "children": 0} for tid in ids}
    for pid, n in conn.execute(
            f"SELECT parent_id, COUNT(*) FROM task_links WHERE parent_id IN ({ph}) GROUP BY parent_id", params):
        counts[pid]["children"] = n
    for cid, n in conn.execute(
            f"SELECT child_id, COUNT(*) FROM task_links WHERE child_id IN ({ph}) GROUP BY child_id", params):
        counts[cid]["parents"] = n
    comments = dict(conn.execute(
        f"SELECT task_id, COUNT(*) FROM task_comments WHERE task_id IN ({ph}) GROUP BY task_id", params))
    progress: dict[str, dict] = {}
    for pid, status in conn.execute(
            "SELECT l.parent_id, t.status FROM task_links l JOIN tasks t ON t.id = l.child_id "
            f"WHERE l.parent_id IN ({ph})", params):
        p = progress.setdefault(pid, {"done": 0, "total": 0})
        p["total"] += 1
        p["done"] += status == "done"
    return {tid: {"link_counts": counts[tid], "comment_count": comments.get(tid, 0),
                  "progress": progress.get(tid)} for tid in ids}


def _affected(conn, cards, pcards, changed):
    """P2 marks both deleted-link endpoints; surviving edges add direct neighbours."""
    affected = set(changed) | (set(cards) - set(pcards))
    if changed:
        ph, params = _ph(changed), tuple(changed)
        for own, other in (("parent_id", "child_id"), ("child_id", "parent_id")):
            affected.update(r[0] for r in conn.execute(
                f"SELECT {other} FROM task_links WHERE {own} IN ({ph})", params))
    return affected & set(cards)


def _extras(conn, cards, pcards, affected, board_read):
    # current_run_id can point at another task's run; keep this cheap PK join exact
    # even when the changed run's owning task is outside the displayed graph.
    starts = kanban_db.current_run_started_ats(conn, list(cards))
    retained = {tid: {**pcards[tid][2], "current_run_started_at": starts.get(tid)}
                for tid in cards if tid not in affected}
    ids = sorted(affected)
    if not ids:
        return retained
    rollups = _rollups(conn, sorted(affected)) if affected else {}
    ph, params = _ph(ids), tuple(ids)
    summaries = kanban_db.latest_summaries(conn, ids)
    try:
        managers = {str(r[0]): str(r[1]) for r in conn.execute(
            "SELECT task_id, chat_id FROM kanban_notify_subs WHERE platform = 'tui' AND chat_id != '' "
            f"AND task_id IN ({ph}) ORDER BY created_at, rowid", params)}
    except sqlite3.Error:  # board predates notify subscriptions
        managers = {}
    cap = board_read._CARD_SUMMARY_PREVIEW_CHARS
    return {**retained, **{tid: {
        **(rollups[tid] if tid in rollups else pcards[tid][2]),
        "latest_summary": summaries[tid][:cap] if summaries.get(tid) else None,
        "current_run_started_at": starts.get(tid),
        "managing_session_id": managers.get(tid) or cards[tid][0].session_id or None} for tid in ids}}


def _done_window(inc, by_id, changed, cap):
    """Update the retained ranking; decline whenever an unseen card might enter the window."""
    done_ids = inc["done_ids"]
    membership_changes = {tid for tid in changed
                          if (tid in done_ids) != (tid in by_id and by_id[tid]["status"] == "done")}
    if membership_changes:
        done_ids = set(done_ids)
    entries = dict(inc["window"])
    truncated = len(done_ids) > cap
    cutoff = inc["window"][-1] if inc["window"] else ("", None)
    for tid in changed:
        row = by_id.get(tid)
        if row is None or row["status"] != "done":
            if tid in entries and truncated:
                return None
            entries.pop(tid, None)
            if tid in membership_changes:
                done_ids.discard(tid)
            continue
        item = (tid, done_epoch(row))
        if tid in entries and truncated and _cmp(item, cutoff) > 0:
            return None
        if tid not in entries and not truncated and tid in done_ids:
            return None
        if tid in entries or not truncated or _cmp(item, cutoff) < 0:
            entries[tid] = item[1]
        if tid in membership_changes:
            done_ids.add(tid)
    window = tuple(sorted(entries.items(), key=rank_key)[:cap])
    if any(tid not in by_id or by_id[tid]["status"] != "done" for tid, _ep in window):
        return None
    return frozenset(done_ids), window


def _card_order(conn, filters, by_id, pcards, changed, window):
    # Even a hidden/out-of-filter waiter can change an ancestor's inherited
    # priority transitively. Re-read narrow dispatch IDs on every activity delta.
    # This also covers deleted links: P2 marks both endpoints, even after cascade.
    if changed:
        nondone = sorted(s for s in kanban_db.VALID_STATUSES if s != "done")
        nd_ids = [kanban_db._lossy_text(r["id"]) for r in kanban_db.list_task_rows(
            conn, columns="id", extra_where=f"status IN ({_ph(nondone)})", extra_params=tuple(nondone), **filters)]
        expect = {tid for tid, row in by_id.items() if row["status"] != "done"}
        if set(nd_ids) != expect or len(nd_ids) != len(expect):
            return None
    else:
        nd_ids = [tid for tid, old in pcards.items() if old[0].status != "done"]
    return nd_ids + [w[0] for w in window]


def _parse_cards(by_id, order, memo):
    """Only parse changed tuples, and retain no hidden history in the parse memo."""
    old_tasks = memo["tasks"]
    parsed, raws, cards = {}, {}, {}
    for tid in order:
        row = by_id[tid]
        raw = (tuple(row.keys()), tuple(row))
        hit = old_tasks.get(raw)
        if hit is None:
            task = kanban_db.Task.from_row(row)
            hit = (task, asdict(task))
        if hit[0].id != tid:
            return None
        parsed[raw], raws[tid], cards[tid] = hit, raw, hit
    return cards, parsed, raws


def _diagnostics(conn, cards, by_id, prev, affected, diagnostic_inputs, br):
    """Use the freshly read task rows even for unmarked ready/done bookkeeping."""
    diag_rows = [by_id[tid] for tid, (task, _fields) in cards.items() if task.status != "archived"]
    if not diag_rows:
        return None, {}
    # All displayed marks/runs and graph contexts are bounded by open rows plus
    # the done cap. Event histories alone are selectively re-read.
    old_graph = prev["diag_inputs"][3] if prev["diag_inputs"] else {}
    ids = [r["id"] for r in diag_rows]
    refresh = [tid for tid in ids if tid in affected or tid not in old_graph]
    graphs = {tid: old_graph[tid] for tid in ids if tid not in refresh}
    if refresh:
        graphs.update(kanban_db.task_graph_contexts(conn, refresh))
    return br._incremental_diag_inputs(conn, diag_rows, prev["_memo"]["diag"], diagnostic_inputs,
                                       graphs=graphs, event_changed=affected)


def _pickers(conn, prev, changed):
    tenants, assignees = prev["tenants"], prev["task_assignees"]
    if not changed:
        return tenants, assignees
    seen = conn.execute(
        f"SELECT tenant, assignee, status FROM tasks WHERE id IN ({_ph(changed)})", tuple(changed)).fetchall()
    tset = set(tenants) | {r[0] for r in seen if r[0] is not None}
    aset = set(assignees) | {r[1] for r in seen if r[1] is not None and r[2] != "archived"}
    tset = {t for t in tset if conn.execute("SELECT 1 FROM tasks WHERE tenant = ? LIMIT 1", (t,)).fetchone()}
    aset = {a for a in aset if conn.execute(
        "SELECT 1 FROM tasks WHERE assignee = ? AND status != 'archived' LIMIT 1", (a,)).fetchone()}
    return tuple(sorted(tset)), frozenset(aset)


def delta(conn, filters: dict, done_limit, diagnostic_inputs: Callable, prev: dict, changed) -> Optional[dict]:
    from plugins.kanban.dashboard import board_read as br

    if not isinstance(changed, (set, frozenset)):
        return None
    memo = prev.get("_memo") or {}
    inc = memo.get("inc")
    cap = int(done_limit or 0)
    if inc is None or cap <= 0 or inc["filters"] != filters_key(filters) or inc["cap"] != cap:
        return None
    changed = sorted(str(c) for c in changed if c)
    if len(changed) > MAX_CHANGED:
        return None
    pcards = {t.id: (t, f, e) for t, f, e in prev["cards"]}
    retained_rows = ({kanban_db._lossy_text(r["id"]): r for r in prev["diag_inputs"][0]}
                     if prev.get("diag_inputs") else None)
    by_id = _bookkeeping_overlay(conn, filters, pcards, changed, retained_rows)
    if set(pcards) - set(changed) - set(by_id):
        return None
    done = _done_window(inc, by_id, changed, cap)
    if done is None:
        return None
    done_ids, window = done
    order = _card_order(conn, filters, by_id, pcards, changed, window)
    if order is None:
        return None
    parsed_cards = _parse_cards(by_id, order, memo)
    if parsed_cards is None:
        return None
    cards, parsed, raws = parsed_cards
    affected = _affected(conn, cards, pcards, changed)
    extras = _extras(conn, cards, pcards, affected, br)
    diag_inputs, memo_diag = _diagnostics(conn, cards, by_id, prev, affected, diagnostic_inputs, br)
    latest = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()["m"]
    tenants, assignees = _pickers(conn, prev, changed)
    return {
        "cards": tuple((t, fields, extras[tid]) for tid, (t, fields) in cards.items()),
        "diag_inputs": diag_inputs, "done_total": len(done_ids),
        "latest_event_id": int(latest), "tenants": tenants, "task_assignees": assignees,
        "_memo": {"delta": True, "tasks": parsed, "diag": memo_diag, "inc": {
            "filters": inc["filters"], "cap": cap, "done_ids": done_ids,
            "window": window, "raw": raws}},
    }
