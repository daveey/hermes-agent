"""What a contained (held) triage card waits on, read from its ``contained`` episode. [daveey]

A card that contained itself sits in ``triage`` with ``tasks.containment_episode`` set. Only the episode's authority
card (or a producer's new completed run) can release it. Its newest park event is an older, unrelated one, so
``kanban_wait_info`` described it as plain intake triage: not agent-owned, never woken. Once the authority finished
(done/archived) nothing was left to release it (5 of the 6 contained cards on the hub, 2026-11-20).

``read`` returns, per contained card, the facts ``kanban_wait_info.describe_contained`` judges against the clock:
the episode uid, the authority and producer cards with their status (the board file first, else a Board replica),
and when the authority finished. Reuses ``kanban_containment_state.reduce_events`` to pick the unreleased episode.
Read-only; any lookup failure leaves the card undescribed (the old behaviour) rather than guessing.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

TERMINAL = ("done", "archived")


def _json(raw: Any) -> dict:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _local_status(conn: sqlite3.Connection, ids: list[str]) -> dict[str, tuple[str, Optional[float]]]:
    """``{id: (status, finished_at)}`` using the terminal transition, never later comments."""
    out: dict[str, tuple[str, Optional[float]]] = {}
    for tid, status, done_at, last in conn.execute(
            "SELECT t.id, t.status, t.completed_at, (SELECT MAX(e.created_at) FROM task_events e "
            "WHERE e.task_id = t.id AND e.kind = CASE t.status WHEN 'archived' THEN 'archived' "
            "ELSE 'completed' END) FROM tasks t WHERE t.id IN (SELECT value FROM json_each(?))", (json.dumps(ids),)):
        out[str(tid)] = (str(status), last if status == "archived" else done_at)
    return out


def _replica_status(ids: list[str]) -> dict[str, tuple[str, Optional[float]]]:
    out: dict[str, tuple[str, Optional[float]]] = {}
    if not ids:
        return out
    from hermes_cli.kanban_board_replica_refs import _replica_paths, with_reader
    for path in _replica_paths():
        rest = [i for i in ids if i not in out]
        if not rest:
            break

        try:
            out.update(with_reader(path, lambda conn: _local_status(conn, rest)))
        except sqlite3.Error:
            continue
    return out


def statuses(conn: sqlite3.Connection, ids: Iterable[str], *, replica: Any = ...) -> dict[str, tuple[str, Optional[float]]]:
    want = list(dict.fromkeys(ids))
    if not want:
        return {}
    try:
        found = _local_status(conn, want)
    except sqlite3.Error:
        found = {}
    missing = [i for i in want if i not in found]
    if missing:
        try:
            if replica is ...:
                found.update(_replica_status(missing))
            elif replica is not None:
                found.update(_local_status(replica, missing))
        except Exception:  # replica unreadable: unknown status counts as open
            logger.debug("contained wait: replica status lookup failed", exc_info=True)
    return found


# A card in one of these does not progress on its own; a ``todo`` card progresses unless every unmet parent is stuck.
STUCK = ("triage", "blocked")
PARENT_DEPTH = 6


def _unmet(conn: sqlite3.Connection, task: str) -> list[tuple[str, str]]:
    """``[(parent, status)]`` of ``task``'s unmet parents on this board file; a containment-closed archive is unmet
    (as it is for readiness). A parent row this file lacks counts as ``unknown`` (progressing, never guessed stuck)."""
    from hermes_cli.kanban_containment_terminal import parent_status_sql
    status = parent_status_sql(conn, "p")
    rows = conn.execute(f"SELECT l.parent_id, CASE WHEN p.id IS NULL THEN 'unknown' ELSE {status} END "
                        "FROM task_links l LEFT JOIN tasks p ON p.id = l.parent_id WHERE l.child_id = ? "
                        "ORDER BY l.parent_id", (task,)).fetchall()
    return [(str(parent), str(state)) for parent, state in rows if state not in TERMINAL]


def _stuck(conn: sqlite3.Connection, task: str, depth: int, seen: frozenset) -> bool:
    """A ``todo`` card that cannot progress: it has unmet parents and each is stuck (triage, blocked, a closed
    containment, or recursively a stuck ``todo``). Running/ready/scheduled/unknown parents, no parents, a cycle or
    the depth bound all count as progressing: stalled is said only when it is certain."""
    unmet = _unmet(conn, task)
    if not unmet or depth >= PARENT_DEPTH:
        return False
    return all(state in STUCK or (state == "todo" and parent not in seen
                                  and _stuck(conn, parent, depth + 1, seen | {parent}))
               for parent, state in unmet)


def _authority_parents(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict]:
    """``{authority: {parents: [{id, status}], parked: bool}}`` for ``todo`` authorities on this board file."""
    out: dict[str, dict] = {}
    for authority in ids:
        try:
            parents = [{"id": p, "status": st} for p, st in _unmet(conn, authority)]
            out[authority] = {"parents": parents, "parked": _stuck(conn, authority, 0, frozenset({authority}))}
        except sqlite3.Error:
            logger.debug("contained wait: authority parent lookup failed for %s", authority, exc_info=True)
    return out


def _producer_released(conn: sqlite3.Connection, producer: str, since: Any) -> bool:
    """The producer has a completed run newer than the containment (same board file only; elsewhere unknown)."""
    try:
        return bool(conn.execute(
            "SELECT 1 FROM task_runs WHERE task_id = ? AND outcome = 'completed' AND ended_at > ? LIMIT 1",
            (producer, since or 0)).fetchone())
    except sqlite3.Error:
        return False


def _active_episode(conn: sqlite3.Connection, tid: str, marker: str) -> Optional[dict]:
    from hermes_cli.kanban_containment_connections import verifier
    from hermes_cli.kanban_containment_state import reduce_events
    cursor = conn.execute("SELECT uid, task_id, kind, payload, created_at FROM task_events WHERE task_id = ? "
                          "AND kind IN ('contained', 'containment_released', 'containment_closed')", (tid,))
    names = [c[0] for c in cursor.description]
    events = [dict(zip(names, row)) for row in cursor]
    hold = reduce_events(events, verify_release=lambda r, s: verifier(conn, r, s))
    if hold is None or hold.unresolved or marker not in hold.episodes:
        return None
    return next((e for e in events if e["kind"] == "contained" and e["uid"] == marker), None)


def read(conn: sqlite3.Connection, task_ids: Iterable[str], *, replica: Any = ..., display: bool = False) -> dict[str, dict[str, Any]]:
    """``{task_id: contained facts}`` for the held triage cards among ``task_ids``::

        {task, episode, contained_at, authority: {id, status, at, parents?, parked?}, producer: {id, status, released} | None}

    ``at`` is when the authority finished (None while open or unknown). For a ``todo`` authority on this board file,
    ``parents`` lists its unmet parents ``[{id, status}]`` and ``parked`` says none of them can progress. Cards that are not held are absent."""
    ids = [str(t) for t in dict.fromkeys(task_ids)]
    if not ids:
        return {}
    try:
        # [daveey] No separate schema probe: ``PRAGMA table_info(tasks)`` handed Python one row per column (59 on
        # the hub board, each a GIL release in the Desktop backend) on every session-list board read. A board
        # without the column (or the table) fails this statement at prepare time, the same empty answer.
        rows = conn.execute("SELECT id, containment_episode FROM tasks WHERE status = 'triage' "
                            "AND containment_episode IS NOT NULL AND id IN (SELECT value FROM json_each(?))",
                            (json.dumps(ids),)).fetchall()
    except sqlite3.Error:
        return {}
    from hermes_cli.kanban_contained_display import Capture, producer_runs
    capture = Capture(conn, [str(tid) for tid, _ in rows]) if display and rows else None
    stops: dict[str, tuple[dict, dict]] = {}
    for tid, marker in rows:
        try:
            stop = (capture.active(str(tid), str(marker)) if capture is not None
                    else _active_episode(conn, str(tid), str(marker)))
        except Exception:
            logger.debug("contained wait: episode read failed for %s", tid, exc_info=True)
            continue
        spec = _json((_json(stop.get("payload")) if stop else {}).get("policy"))
        authority = (spec.get("authority") or {}).get("task_id")
        if stop is None or not authority:
            continue
        stops[str(tid)] = (stop, spec)
    need = [spec["authority"]["task_id"] for _, spec in stops.values()]
    need += [spec["producer"]["task_id"] for _, spec in stops.values() if isinstance(spec.get("producer"), dict)]
    known = statuses(conn, need) if replica is ... else statuses(conn, need, replica=replica)
    parked = _authority_parents(conn, [a for a in dict.fromkeys(spec["authority"]["task_id"] for _, spec in stops.values())
                                  if known.get(a, ("unknown",))[0] == "todo"])
    latest = producer_runs(conn, stops) if display else {}
    out: dict[str, dict[str, Any]] = {}
    for tid, (stop, spec) in stops.items():
        a_id = spec["authority"]["task_id"]
        a_status, a_at = known.get(a_id, ("unknown", None))
        info: dict[str, Any] = {
            "episode": stop["uid"], "contained_at": stop.get("created_at"),
            "authority": {"id": a_id, "status": a_status, "at": a_at if a_status in TERMINAL else None},
            "producer": None}
        info["task"] = tid
        if a_id in parked:
            info["authority"].update(parked[a_id])
        producer = spec.get("producer")
        if isinstance(producer, dict) and producer.get("task_id"):
            p_id = producer["task_id"]
            p_status = known.get(p_id, ("unknown", None))[0]
            info["producer"] = {"id": p_id, "status": p_status,
                                "released": (bool(latest.get(tid))
                                             if display else _producer_released(conn, p_id, stop.get("created_at")))}
        out[tid] = info
    return out
