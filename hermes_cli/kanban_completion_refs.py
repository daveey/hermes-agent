"""Which receipt event a ``completed`` event names, on the board that reads it. [daveey]

A completion binds its receipts (``pr_acceptance``, ``goal_verified``) by ``task_events.id``. That integer is
local to one board file. A card homed on the hub but run on a pool worker completes on the worker's board; the
Board feed replays its events onto the hub with new ids (``task_events.run_id`` is remapped through ``run_uid``,
the JSON payload is not), so on the hub the payload's id names another card's event or none. The hub's
``completion_verdict`` then never verified the completion, the origin ledger never released it, and the origin
chat listed the done card as ``unknown`` for hours (hub t_01a1d41478234144 et al., 2026-11-13; the same class as
``writer_run_uid`` in ``kanban_db_dispatch_pr_state``).

The completion now also records each receipt's stable ``uid`` (``*_event_uid``), which the feed carries
unchanged; when present it decides, with no integer fallback. A completion written before that, replayed from
another gateway (``origin_gateway`` set), whose integer does not name the same card's receipt here, binds the
latest receipt of that kind the same gateway wrote in the same run before the completion: on its own board that
run's latest receipt is the one ``complete_task`` bound (it writes the receipt and the completion in one
transaction). A completion that names no receipt never borrows one.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

RECEIPT_KINDS = ("pr_acceptance", "goal_verified")
UID_KEYS = {"pr_acceptance": "pr_acceptance_event_uid", "goal_verified": "goal_verified_event_uid"}
ID_KEYS = {"pr_acceptance": "pr_acceptance_event_id", "goal_verified": "goal_verified_event_id"}
_FIELDS = ("id", "uid", "task_id", "kind", "run_id", "origin_gateway", "payload")


def stamp_uids(conn: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Add ``*_event_uid`` beside each receipt id in a completion payload, when this board stamps uids."""
    for kind, id_key in ID_KEYS.items():
        event_id = payload.get(id_key)
        if type(event_id) is not int:
            continue
        try:
            row = conn.execute("SELECT uid FROM task_events WHERE id = ?", (event_id,)).fetchone()
        except sqlite3.OperationalError:  # an unsynced board has no uid column: the id is the only name
            return
        if row is not None and isinstance(row[0], str) and row[0]:
            payload[UID_KEYS[kind]] = row[0]


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}


def read_candidates(conn: sqlite3.Connection, task_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """``{task_id: [receipt event rows]}``: every ``pr_acceptance``/``goal_verified`` event of these cards."""
    ids = list(dict.fromkeys(str(t) for t in task_ids))
    out: dict[str, list[dict[str, Any]]] = {t: [] for t in ids}
    if not ids:
        return out
    cols = _columns(conn)
    select = ",".join(f"e.{c}" if c in cols else f"NULL AS {c}" for c in _FIELDS)
    marks = ",".join("?" for _ in ids)
    for row in conn.execute(f"SELECT {select} FROM task_events e WHERE e.task_id IN ({marks}) "
                            "AND e.kind IN ('pr_acceptance','goal_verified')", ids):
        out[str(row[2])].append(dict(zip(_FIELDS, tuple(row))))
    return out


def origin_of(conn: sqlite3.Connection, event_id: int) -> str | None:
    """The gateway a replayed event came from (``task_events.origin_gateway``), else None (written here)."""
    if "origin_gateway" not in _columns(conn):
        return None
    row = conn.execute("SELECT origin_gateway FROM task_events WHERE id = ?", (event_id,)).fetchone()
    return row[0] if row is not None and isinstance(row[0], str) and row[0] else None


def pick(rows: Iterable[dict[str, Any]], *, task_id: str, kind: str, event_id: Any, uid: Any, before: int,
         run_id: Any, imported_from: str | None) -> tuple[Any, Any] | None:
    """``(payload, run_id)`` of the receipt a completion names, or None. See the module docstring."""
    mine = [r for r in rows if r["task_id"] == task_id and r["kind"] == kind and type(r["id"]) is int
            and r["id"] < before]
    if isinstance(uid, str) and uid:
        hit = next((r for r in mine if r["uid"] == uid), None)
        return None if hit is None else (hit["payload"], hit["run_id"])
    if type(event_id) is not int:
        return None
    hit = next((r for r in mine if r["id"] == event_id), None)
    if hit is not None and (imported_from is None or hit["origin_gateway"] == imported_from):
        return hit["payload"], hit["run_id"]
    if imported_from is None or run_id is None:
        return None
    same_run = [r for r in mine if r["run_id"] == run_id and r["origin_gateway"] == imported_from]
    if not same_run:
        return None
    best = max(same_run, key=lambda r: r["id"])
    return best["payload"], best["run_id"]
