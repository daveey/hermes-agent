"""Deterministic hub admission score (phase 2 of docs/kanban-hub-admission-v2.md). [daveey]

``score = min(PRIORITY_CAP, effective_priority) + class * class_rank + unblock * min(open_descendants, 20)
         + age * ready_minutes - failure * consecutive_failures``

Effective priority and open descendants come from the same graph as
:func:`kanban_dispatch_priority.order_with_remote_waiters`: the full replica, with local rows and a local child's
edge set overriding it. Readiness age is the current ready/review interval (the stranded-ready diagnostic's floor:
latest ready-entry event, latest run end, else creation), not card age. One call is one snapshot: nothing is cached
or written (stored priority included), so completion, unlink or archive take effect on the next call.

Turn owners rank first. A card the local turn-urgency observation (:mod:`kanban_turn_urgency`) names as holding the
core land lane or a state land turn sorts ahead of every other card; owners keep score order among themselves. The
score itself is unchanged (the 900 inherited floor still applies for donation). The floor alone could not do this:
at the 1000 cap the age term (25 points per ready minute) decides, so an owner just back in ready from a CI or
review park ranked behind every older card while its turn ran out (PR #3207, t_01a1e023229f1073). A stale, invalid
or missing observation names no owners, and the order is the score alone.
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
DESCENDANT_CAP, AGE_BUCKET_S = 20, 60


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
    turn_owner: bool = False  # holds a land turn/lane per the turn-urgency observation: ranks ahead of any score

    def as_dict(self) -> dict:
        return {"effective_priority": self.effective_priority, "class_rank": self.class_rank,
                "descendants": self.descendants, "age_buckets": self.age_buckets, "failures": self.failures,
                "total": self.total, "ready_since": self.ready_since, "weights": dict(self.weights),
                "turn_owner": self.turn_owner}


def _n(value) -> str:
    return f"{value:g}"


def explain(parts: dict) -> str:
    """``score T = priority P + class W*r + unblock W*d + age W*a - failures W*f`` for an ``as_dict`` breakdown."""
    w = parts["weights"]
    return (f"score {_n(parts['total'])} = priority {parts['effective_priority']} + class {_n(w['class'])}*"
            f"{parts['class_rank']} + unblock {_n(w['unblock'])}*{parts['descendants']} + age {_n(w['age'])}*"
            f"{parts['age_buckets']} - failures {_n(w['failure'])}*{parts['failures']}{_owner_note(parts)}")


def summary(parts: dict) -> str:
    """Points per component for queue notes: ``score T (priority P + class C + unblock U + age A - failures F)``."""
    w = parts["weights"]
    return (f"score {_n(parts['total'])} (priority {parts['effective_priority']} + class "
            f"{_n(w['class'] * parts['class_rank'])} + unblock {_n(w['unblock'] * parts['descendants'])} + age "
            f"{_n(w['age'] * parts['age_buckets'])} - failures {_n(w['failure'] * parts['failures'])})"
            f"{_owner_note(parts)}")


def _owner_note(parts: dict) -> str:
    return "; turn owner: ranks ahead of non-owners" if parts.get("turn_owner") else ""


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


def _graph(conn: sqlite3.Connection, candidate_ids, *, now_s=None):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn

    remote, edges = [], []
    metadata, since = {}, {}
    if sync.board_mode(conn) == "primary":
        replica = None
        try:
            try:
                replica = wn.open_replica(conn)
            except RuntimeError:
                # The replica provider reports unavailable runtime state this way;
                # do not swallow RuntimeError from graph/history implementation.
                replica = None
            if replica is not None:
                remote, edges, metadata, since = _source_snapshot(replica, candidate_ids)
        except (sqlite3.Error, OSError):
            # Never retain graph fragments after a failed history/edge read.
            remote, edges, metadata, since = [], [], {}, {}
        finally:
            if replica is not None:
                replica.close()
    local, local_edges, local_metadata, local_since = _source_snapshot(conn, candidate_ids)
    local_ids = {r[0] for r in local}
    tasks = {r[0]: (r[1] or 0, r[2]) for r in remote}
    tasks.update((r[0], (r[1] or 0, r[2])) for r in local)
    owners = _apply_turn_floor(conn, tasks, now_s=now_s)
    edges = [(p, c) for p, c in edges if c not in local_ids]
    edges.extend(tuple(r) for r in local_edges)
    children = defaultdict(set)
    for parent, child in edges:
        if child in tasks and tasks[child][1] not in ("done", "archived"):
            children[parent].add(child)
    metadata.update(local_metadata)
    since.update(local_since)
    return tasks, children, metadata, since, owners


def _apply_turn_floor(conn, tasks, *, now_s=None) -> frozenset:
    """Apply advisory owners only to existing open rows on this connection's board; return those owners."""
    import time

    from hermes_cli.kanban_host_counts import board_of
    from hermes_cli.kanban_turn_urgency import read_owners

    board = board_of(conn)
    if board is None:
        return frozenset()
    owners = read_owners(board=board, now_s=time.time() if now_s is None else now_s)
    applied = set()
    for tid in owners & tasks.keys():
        priority, status = tasks[tid]
        if status not in ("done", "archived"):
            tasks[tid] = (max(priority, 900), status)
            applied.add(tid)
    return frozenset(applied)


ALARM_KEY_PREFIXES = ("worker-claim-watch:", "pickup-stall:", "mods-suite-red:")


def _alarm_key(key) -> bool:
    """Explicit detector keys, not titles or priority bands, identify alarms."""
    return isinstance(key, str) and key.startswith(ALARM_KEY_PREFIXES)


def _source_snapshot(conn, candidate_ids):
    """One consistent source view without committing a caller-owned transaction.

    The two databases cannot share an atomic snapshot; local custody wins when
    merging them. A savepoint pins each source's graph and history together.
    """
    conn.execute("SAVEPOINT admission_score_read")
    try:
        # Minimal/older replicas can lack producer identity. Absence gives no
        # urgency floor; never infer an alarm from user-editable title prose.
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        key_column = "idempotency_key" if "idempotency_key" in columns else "NULL"
        tasks = [(tid, max(priority or 0, 900) if _alarm_key(key) else priority, status)
                 for tid, priority, status, key in conn.execute(
                     f"SELECT id, priority, status, {key_column} FROM tasks")]
        edges = conn.execute("SELECT parent_id, child_id FROM task_links").fetchall()
        metadata, since = _candidate_state(conn, candidate_ids)
        return tasks, edges, metadata, since
    finally:
        conn.execute("RELEASE admission_score_read")


def _candidate_state(conn, ids):
    """Read candidate metadata/history from one source, in bounded SQL batches."""
    metadata, since = {}, {}
    for offset in range(0, len(ids), 400):
        batch = ids[offset:offset + 400]
        marks = ",".join("?" for _ in batch)
        found = {r[0]: (r[1] or "normal", r[2] or 0, r[3] or 0) for r in conn.execute(
            f"SELECT id, admission_class, consecutive_failures, created_at FROM tasks WHERE id IN ({marks})", batch)}
        metadata.update(found)
        # Restrict history to extant rows; orphan events do not restore a vanished candidate.
        since.update(_ready_since(conn, list(found)))
    return metadata, since


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
    from hermes_cli.kanban_ready_events import READY_WAIT_FLOOR_KINDS as _READY_WAIT_FLOOR_KINDS

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


def effective_priorities(conn: sqlite3.Connection, task_ids: list[str]) -> dict[str, int]:
    """Creation-time urgency inheritance, excluding age/class/unblock/failure points.

    Return only extant task identities. Stored values are never changed.
    """
    tasks, children, _metadata, _since, _owners = _graph(conn, [])
    return {tid: min(PRIORITY_CAP, max([tasks[tid][0], *(tasks[c][0] for c in _descendants(tid, children))]))
            for tid in task_ids if tid in tasks}


def rank(conn: sqlite3.Connection, rows, *, now_ms: Optional[int] = None, weights: Optional[dict] = None):
    """``rows`` (local task rows with ``id``) in admission order, each with its :class:`ScoreBreakdown`.

    Pure and read-only. Turn owners come first (module docstring), then the score; ties break on
    ready-interval start, source creation time, then task id. Creation retains subsecond precision and local
    custody wins."""
    import time

    rows = list(rows)
    if not rows:
        return []
    weights = configured_weights() if weights is None else validate_weights(weights)
    now_s = (time.time() * 1000 if now_ms is None else now_ms) / 1000
    ids = [row["id"] for row in rows]
    tasks, children, metadata, since, owners = _graph(conn, ids, now_s=now_s)
    scored = []
    for row in rows:
        tid = row["id"]
        below = _descendants(tid, children)
        priority = max([tasks.get(tid, (0, None))[0], *(tasks[c][0] for c in below)])
        klass, failures, _created = metadata.get(tid, ("normal", 0, 0))
        class_rank = CLASS_RANK.get(klass, 0)
        start = since.get(tid, 0)
        if start <= 0:
            start = int(now_s)
        buckets = max(0, int(now_s - start)) // AGE_BUCKET_S
        descendants = min(DESCENDANT_CAP, len(below))
        effective = min(PRIORITY_CAP, priority)
        total = (effective + weights["class"] * class_rank + weights["unblock"] * descendants
                 + weights["age"] * buckets - weights["failure"] * failures)
        scored.append((row, ScoreBreakdown(effective, class_rank, descendants, buckets, failures, total, start,
                                           tuple(sorted(weights.items())), tid in owners)))
    scored.sort(key=lambda item: (not item[1].turn_owner, -item[1].total, item[1].ready_since,
                                 metadata.get(item[0]["id"], ("normal", 0, 0))[2], item[0]["id"]))
    from hermes_cli.kanban_lanes import order

    return order(conn, scored)
