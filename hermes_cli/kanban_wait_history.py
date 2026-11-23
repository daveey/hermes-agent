"""How long CI and deploy waits took before, from the board's own event history. [daveey]

A wait starts at a ``scheduled`` / ``continued`` park event whose ``awaiting`` names the kind (``ci`` or ``deploy``)
and a target (the repo of a CI run, the host/service of a deploy: ``kanban_wait_info.wait_target``). It finished
when the card's next wake event arrived (``END_EVENTS``). ``samples`` returns the durations of the newest
``ETA_HISTORY`` finished waits of one kind and target; ``overlay`` fills the ETA fields of the CI / deploy waits in
a wait-info map from them. Read-only; a board without ``task_events`` yields no samples.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

from hermes_cli import kanban_wait_info as kwi

# The first of these after a park ends the wait: the wake check passed, the continuation woke, the card was
# promoted or unblocked, a worker claimed it, or it finished / was archived while parked.
END_EVENTS = ("condition_met", "continuation_woken", "promoted", "promoted_manual", "unblocked", "claimed",
              "completed", "archived")
_PARK_KINDS = ("scheduled", "continued")
SCAN_CAP = 600  # newest park events of the kind read per lookup
_TTL_S = 60.0
_lock = threading.Lock()
_memo: dict[tuple[str, str, str], tuple[float, list[float]]] = {}


def _db_key(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row[2] or id(conn)) if row else str(id(conn))
    except sqlite3.Error:
        return str(id(conn))


def samples(conn: sqlite3.Connection, kind: str, target: str, *, now: Optional[float] = None) -> list[float]:
    """Durations (seconds, newest first) of up to ``ETA_HISTORY`` finished waits of ``kind`` on ``target``."""
    if kind not in ("ci", "deploy") or not target:
        return []
    wall = time.time() if now is None else now
    key = (_db_key(conn), kind, target)
    with _lock:
        hit = _memo.get(key)
        if hit and 0 <= wall - hit[0] < _TTL_S:
            return list(hit[1])
    out: list[float] = []
    try:
        ends = ",".join("?" for _ in END_EVENTS)
        parks = ",".join("?" for _ in _PARK_KINDS)
        rows = conn.execute(
            f"SELECT e.payload, e.created_at, (SELECT n.created_at FROM task_events n WHERE n.task_id = e.task_id "
            f"AND n.id > e.id AND n.kind IN ({ends}) ORDER BY n.id LIMIT 1) "
            f"FROM task_events e WHERE e.kind IN ({parks}) AND json_extract(e.payload, '$.awaiting.kind') = ? "
            f"ORDER BY e.id DESC LIMIT ?", (*END_EVENTS, *_PARK_KINDS, kind, SCAN_CAP)).fetchall()
        for payload, parked_at, ended_at in rows:
            if ended_at is None or parked_at is None or float(ended_at) <= float(parked_at):
                continue
            try:
                awaiting = json.loads(payload).get("awaiting") or {}
            except (ValueError, AttributeError, TypeError):
                continue
            if kwi.wait_target(kind, awaiting.get("ref")) != target:
                continue
            out.append(float(ended_at) - float(parked_at))
            if len(out) >= kwi.ETA_HISTORY:
                break
    except sqlite3.Error:  # no task_events, or a SQLite without json_extract: no history, never an error
        return []
    with _lock:
        _memo[key] = (wall, list(out))
    return out


def overlay(conn: sqlite3.Connection, info: dict[str, dict[str, Any]], now: Optional[float] = None) -> None:
    """Fill ``expected_s`` / ``estimate_source`` of the ci / deploy waits in ``info`` from the board's history, in
    place. A wait without a comparable target keeps ``no history yet``. Never raises."""
    for wait in info.values():
        kind = wait.get("wait_kind")
        if kind not in ("ci", "deploy") or not wait.get("wait_target"):
            continue
        try:
            found = samples(conn, kind, wait["wait_target"], now=now)
        except (sqlite3.Error, ValueError, TypeError):  # display-only: a board without history keeps "no history yet"
            continue
        wait.update(kwi.eta_fields(kind, wait.get("waited_since"), samples=found, target=wait["wait_target"]))


def reset_for_tests() -> None:
    with _lock:
        _memo.clear()
