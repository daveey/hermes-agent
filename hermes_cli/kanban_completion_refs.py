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

The hub recorded ``origin_gateway`` only from event 1180961 (2026-11-13); every event replayed before that,
completions and receipts alike, has NULL there, so eight worker-run hub cards (t_01a1ca1975471ed4 et al.) stayed
unverified and their origin chat listed them ``unknown``. A NULL-origin completion counts as replayed (``legacy``)
only when its own run carries the Board's lease (``claimed`` with ``holder`` and that run's ``run_uid``) to a
gateway other than this board's own; it then binds a NULL-origin receipt of the same run. A run this gateway
held, or one with no Board lease (an unsynced board), stays exact.
"""
from __future__ import annotations

import json
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


def leased_elsewhere(conn: sqlite3.Connection, runs: dict[str, Any]) -> dict[str, str]:
    """``{task_id: holder}`` for each ``{task_id: run_id}`` whose run the Board leased to exactly one gateway
    other than this board's own (``claimed`` with ``holder`` and that run's ``run_uid``). One SELECT."""
    runs = {t: r for t, r in runs.items() if type(r) is int}
    if not runs or "uid" not in {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}:
        return {}
    try:
        own = conn.execute("SELECT gateway_id FROM kanban_sync_state LIMIT 1").fetchone()
    except sqlite3.OperationalError:  # an unsynced board: no Board lease, nothing was replayed
        return {}
    own = own[0] if own is not None and isinstance(own[0], str) and own[0] else None
    if own is None:
        return {}
    holders: dict[str, set[Any]] = {}
    marks = ",".join("?" for _ in runs)
    for tid, run_id, uid, raw in conn.execute(
            f"SELECT e.task_id, e.run_id, r.uid, e.payload FROM task_events e JOIN task_runs r ON r.id = e.run_id "
            f"WHERE e.task_id IN ({marks}) AND e.kind = 'claimed'", list(runs)):
        try:
            claim = json.loads(raw or "{}")
        except ValueError:
            continue
        if (run_id == runs.get(tid) and isinstance(uid, str) and uid and isinstance(claim, dict)
                and claim.get("run_uid") == uid and isinstance(claim.get("holder"), str) and claim["holder"]):
            holders.setdefault(tid, set()).add(claim["holder"])
    out: dict[str, str] = {}
    for tid, found in holders.items():
        holder = next(iter(found)) if len(found) == 1 else None  # two holders for one run: ambiguous, exact
        if holder is not None and holder != own:
            out[tid] = holder
    return out


def import_kwargs(conn: sqlite3.Connection, event_id: int, task_id: str, run_id: Any) -> dict[str, Any]:
    """``pick``'s ``imported_from``/``legacy`` for a completion event: its ``origin_gateway``, else its run's
    foreign Board lease."""
    origin = origin_of(conn, event_id)
    if origin is not None:
        return {"imported_from": origin, "legacy": False}
    holder = leased_elsewhere(conn, {task_id: run_id}).get(task_id)
    return {"imported_from": holder, "legacy": holder is not None}


def pick(rows: Iterable[dict[str, Any]], *, task_id: str, kind: str, event_id: Any, uid: Any, before: int,
         run_id: Any, imported_from: str | None, legacy: bool = False) -> tuple[Any, Any] | None:
    """``(payload, run_id)`` of the receipt a completion names, or None. See the module docstring.
    ``legacy``: replayed from ``imported_from`` before origins were recorded, so its receipts carry none."""
    mine = [r for r in rows if r["task_id"] == task_id and r["kind"] == kind and type(r["id"]) is int
            and r["id"] < before]
    if isinstance(uid, str) and uid:
        hit = next((r for r in mine if r["uid"] == uid), None)
        return None if hit is None else (hit["payload"], hit["run_id"])
    if type(event_id) is not int:
        return None
    origin = None if legacy else imported_from
    hit = next((r for r in mine if r["id"] == event_id), None)
    if hit is not None and (imported_from is None or hit["origin_gateway"] == origin):
        return hit["payload"], hit["run_id"]
    if imported_from is None or run_id is None:
        return None
    same_run = [r for r in mine if r["run_id"] == run_id and r["origin_gateway"] == origin]
    if not same_run:
        return None
    best = max(same_run, key=lambda r: r["id"])
    return best["payload"], best["run_id"]
