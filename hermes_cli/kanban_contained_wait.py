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
    """``{id: (status, finished_at)}`` for cards this board file holds. ``finished_at`` is ``completed_at``, else the
    newest event (an archived card keeps no completion time)."""
    out: dict[str, tuple[str, Optional[float]]] = {}
    marks = ",".join("?" * len(ids))
    for tid, status, done_at, last in conn.execute(
            f"SELECT t.id, t.status, t.completed_at, (SELECT MAX(e.created_at) FROM task_events e "
            f"WHERE e.task_id = t.id) FROM tasks t WHERE t.id IN ({marks})", ids):
        out[str(tid)] = (str(status), done_at or last)
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

        def read(conn: sqlite3.Connection, rest=rest) -> list:
            return conn.execute(
                f"SELECT t.id, t.status, t.completed_at, (SELECT MAX(e.created_at) FROM task_events e "
                f"WHERE e.task_id = t.id) FROM tasks t WHERE t.id IN ({','.join('?' * len(rest))})", rest).fetchall()

        try:
            for tid, status, done_at, last in with_reader(path, read):
                out[str(tid)] = (str(status), done_at or last)
        except sqlite3.Error:
            continue
    return out


def statuses(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, tuple[str, Optional[float]]]:
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
            found.update(_replica_status(missing))
        except Exception:  # replica unreadable: unknown status counts as open
            logger.debug("contained wait: replica status lookup failed", exc_info=True)
    return found


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
                          "AND kind IN ('contained', 'containment_released')", (tid,))
    names = [c[0] for c in cursor.description]
    events = [dict(zip(names, row)) for row in cursor]
    hold = reduce_events(events, verify_release=lambda r, s: verifier(conn, r, s))
    if hold is None or hold.unresolved or marker not in hold.episodes:
        return None
    return next((e for e in events if e["kind"] == "contained" and e["uid"] == marker), None)


def read(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """``{task_id: contained facts}`` for the held triage cards among ``task_ids``::

        {episode, contained_at, authority: {id, status, at}, producer: {id, status, released} | None}

    ``at`` is when the authority finished (None while open or unknown). Cards that are not held are absent."""
    ids = [str(t) for t in dict.fromkeys(task_ids)]
    if not ids:
        return {}
    try:
        if "containment_episode" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
            return {}
        marks = ",".join("?" * len(ids))
        rows = conn.execute(f"SELECT id, containment_episode FROM tasks WHERE status = 'triage' "
                            f"AND containment_episode IS NOT NULL AND id IN ({marks})", ids).fetchall()
    except sqlite3.Error:
        return {}
    stops: dict[str, tuple[dict, dict]] = {}
    for tid, marker in rows:
        try:
            stop = _active_episode(conn, str(tid), str(marker))
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
    known = statuses(conn, need)
    out: dict[str, dict[str, Any]] = {}
    for tid, (stop, spec) in stops.items():
        a_id = spec["authority"]["task_id"]
        a_status, a_at = known.get(a_id, ("unknown", None))
        info: dict[str, Any] = {
            "episode": stop["uid"], "contained_at": stop.get("created_at"),
            "authority": {"id": a_id, "status": a_status, "at": a_at if a_status in TERMINAL else None},
            "producer": None}
        producer = spec.get("producer")
        if isinstance(producer, dict) and producer.get("task_id"):
            p_id = producer["task_id"]
            p_status = known.get(p_id, ("unknown", None))[0]
            info["producer"] = {"id": p_id, "status": p_status,
                                "released": _producer_released(conn, p_id, stop.get("created_at"))}
        out[tid] = info
    return out
