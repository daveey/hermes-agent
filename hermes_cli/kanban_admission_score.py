"""Deterministic hub admission score (phase 2 of docs/kanban-hub-admission-v2.md). [daveey]

``score = min(PRIORITY_CAP, effective_priority) + class * class_rank + unblock * min(open_descendants, 20)
         + age * min(ready_minutes // 10, 60) - failure * consecutive_failures``

Effective priority and open descendants come from the same graph as
:func:`kanban_dispatch_priority.order_with_remote_waiters`: the full replica, with local rows and a local child's
edge set overriding it. Readiness age is the current ready/review interval (the stranded-ready diagnostic's floor:
latest ready-entry event, latest run end, else creation), not card age. One call is one snapshot: nothing is cached
or written (stored priority included), so completion, unlink or archive take effect on the next call.
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from hermes_cli.kanban_priority import PRIORITY_CAP

DEFAULT_WEIGHTS = {"class": 500, "unblock": 25, "age": 25, "failure": 100}
CLASS_RANK = {"normal": 0, "capacity": 1, "urgent": 2}
DESCENDANT_CAP, AGE_BUCKET_S, AGE_CAP = 20, 600, 60


@dataclass(frozen=True)
class ScoreBreakdown:
    effective_priority: int
    class_rank: int
    descendants: int
    age_buckets: int
    failures: int
    total: float
    ready_since: int
    weights: tuple = tuple(sorted(DEFAULT_WEIGHTS.items()))

    def as_dict(self) -> dict:
        return {"effective_priority": self.effective_priority, "class_rank": self.class_rank,
                "descendants": self.descendants, "age_buckets": self.age_buckets, "failures": self.failures,
                "total": self.total, "ready_since": self.ready_since, "weights": dict(self.weights)}


def _n(value) -> str:
    return f"{value:g}"


def explain(parts: dict) -> str:
    """``score T = priority P + class W*r + unblock W*d + age W*a - failures W*f`` for an ``as_dict`` breakdown."""
    w = parts["weights"]
    return (f"score {_n(parts['total'])} = priority {parts['effective_priority']} + class {_n(w['class'])}*"
            f"{parts['class_rank']} + unblock {_n(w['unblock'])}*{parts['descendants']} + age {_n(w['age'])}*"
            f"{parts['age_buckets']} - failures {_n(w['failure'])}*{parts['failures']}")


def summary(parts: dict) -> str:
    """Points per component for queue notes: ``score T (priority P + class C + unblock U + age A - failures F)``."""
    w = parts["weights"]
    return (f"score {_n(parts['total'])} (priority {parts['effective_priority']} + class "
            f"{_n(w['class'] * parts['class_rank'])} + unblock {_n(w['unblock'] * parts['descendants'])} + age "
            f"{_n(w['age'] * parts['age_buckets'])} - failures {_n(w['failure'] * parts['failures'])})")


def validate_weights(raw) -> dict:
    """``kanban.admission.weights``: a mapping of known keys to nonnegative numbers; absent keys use defaults."""
    if raw is None:
        return dict(DEFAULT_WEIGHTS)
    if not isinstance(raw, dict):
        raise ValueError("admission weights must be a mapping")
    unknown = sorted(set(raw) - set(DEFAULT_WEIGHTS))
    if unknown:
        raise ValueError(f"unknown admission weight(s): {', '.join(map(str, unknown))}")
    out = dict(DEFAULT_WEIGHTS)
    for key, value in raw.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"admission weight {key} must be a nonnegative number")
        out[key] = value
    return out


def configured_weights() -> dict:
    """Root ``kanban.admission.weights`` (validated)."""
    from hermes_cli.kanban_admission_pools import admission_policy

    return validate_weights(admission_policy().get("weights"))


def _graph(conn: sqlite3.Connection):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn

    remote, edges = [], []
    if sync.board_mode(conn) == "primary":
        replica = wn.open_replica(conn)
        if replica is not None:
            try:
                remote = replica.execute(
                    "SELECT id, priority, status FROM tasks WHERE status NOT IN ('done', 'archived')").fetchall()
                edges = replica.execute("SELECT parent_id, child_id FROM task_links").fetchall()
            finally:
                replica.close()
    local = conn.execute("SELECT id, priority, status FROM tasks").fetchall()
    local_ids = {r[0] for r in local}
    tasks = {r[0]: (r[1] or 0, r[2]) for r in remote}
    tasks.update((r[0], (r[1] or 0, r[2])) for r in local)
    edges = [(p, c) for p, c in edges if c not in local_ids]
    edges.extend(tuple(r) for r in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall())
    children = defaultdict(set)
    for parent, child in edges:
        if child in tasks and tasks[child][1] not in ("done", "archived"):
            children[parent].add(child)
    return tasks, children


def _descendants(tid, children):
    seen, pending = {tid}, list(children.get(tid, ()))
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        pending.extend(children.get(child, ()))
    seen.discard(tid)
    return seen


def _ready_since(conn: sqlite3.Connection, ids: list[str]) -> dict[str, int]:
    from hermes_cli.kanban_diagnostics import _READY_WAIT_FLOOR_KINDS

    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    kinds = sorted(_READY_WAIT_FLOOR_KINDS)
    kind_marks = ",".join("?" for _ in kinds)
    since = {r[0]: int(r[1] or 0) for r in conn.execute(
        f"SELECT id, created_at FROM tasks WHERE id IN ({marks})", ids)}
    for tid, ts in conn.execute(
        f"SELECT task_id, MAX(created_at) FROM task_events WHERE task_id IN ({marks}) AND (kind IN ({kind_marks}) "
        "OR (kind = 'continued' AND json_extract(payload, '$.status') = 'ready')) GROUP BY task_id",
        (*ids, *kinds)):
        since[tid] = max(since.get(tid, 0), int(ts or 0))
    for tid, ts in conn.execute(
        f"SELECT task_id, MAX(ended_at) FROM task_runs WHERE task_id IN ({marks}) GROUP BY task_id", ids):
        since[tid] = max(since.get(tid, 0), int(ts or 0))
    return since


def rank(conn: sqlite3.Connection, rows, *, now_ms: Optional[int] = None, weights: Optional[dict] = None):
    """``rows`` (local task rows with ``id``) in admission order, each with its :class:`ScoreBreakdown`.

    Pure and read-only. Ties break on the earlier ready-interval start, then task id."""
    import time

    rows = list(rows)
    if not rows:
        return []
    weights = configured_weights() if weights is None else validate_weights(weights)
    now_s = (time.time() * 1000 if now_ms is None else now_ms) / 1000
    tasks, children = _graph(conn)
    ids = [row["id"] for row in rows]
    marks = ",".join("?" for _ in ids)
    local = {r[0]: (r[1] or "normal", r[2] or 0) for r in conn.execute(
        f"SELECT id, admission_class, consecutive_failures FROM tasks WHERE id IN ({marks})", ids)}
    since = _ready_since(conn, ids)
    scored = []
    for row in rows:
        tid = row["id"]
        below = _descendants(tid, children)
        priority = max([tasks.get(tid, (0, None))[0], *(tasks[c][0] for c in below)])
        klass, failures = local.get(tid, ("normal", 0))
        class_rank = CLASS_RANK.get(klass, 0)
        start = since.get(tid, 0)
        buckets = min(AGE_CAP, max(0, int(now_s - start)) // AGE_BUCKET_S)
        descendants = min(DESCENDANT_CAP, len(below))
        effective = min(PRIORITY_CAP, priority)
        total = (effective + weights["class"] * class_rank + weights["unblock"] * descendants
                 + weights["age"] * buckets - weights["failure"] * failures)
        scored.append((row, ScoreBreakdown(effective, class_rank, descendants, buckets, failures, total, start,
                                           tuple(sorted(weights.items())))))
    scored.sort(key=lambda item: (-item[1].total, item[1].ready_since, item[0]["id"]))
    return scored
