"""Parked-card wait rows and wait info, split from ``kanban_session_activity``.

Pure readers over one connection: ``_local_wait_rows`` reads the stored parked rows and newest park events,
``_describe_waits`` turns them into wait info (re-judged against the clock on each use), ``_local_wait_info``
composes both. [daveey]
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Iterable


def _stored_needs_human(value, kind) -> bool:
    """Decode the DB's JSON text before applying the shared evidence contract."""
    from hermes_cli.kanban_human_request import needs_human

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    return needs_human(value, kind=kind)


def _local_wait_info(conn: sqlite3.Connection, task_ids: Iterable[str], now: float | None = None,
                     ) -> dict[str, dict[str, Any]]:
    """[daveey] ``{task_id: wait info}`` (``kanban_wait_info.describe``) for the parked cards among ``task_ids``: what
    each waits on, since when, and whether only an agent can end it. One query for the cards, one for their newest
    park events. Missing columns/tables degrade to status-only info."""
    return _describe_waits(*_local_wait_rows(conn, task_ids), now=now)


def _local_wait_rows(conn: sqlite3.Connection, task_ids: Iterable[str]) -> tuple[dict[str, tuple], dict[str, tuple]]:
    """``(parked, events)``: the stored rows ``_describe_waits`` turns into wait info. Separate so a board memo can
    keep the rows and re-judge their age at each read (the stall cut-off moves with the clock, the rows do not)."""
    from hermes_cli import kanban_wait_info as kwi

    ids = [str(t) for t in dict.fromkeys(task_ids)]
    if not ids:
        return {}, {}
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    pick = ", ".join(f"t.{c}" if c in cols else f"NULL AS {c}" for c in ("id", "status", "block_kind", "human_request",
                                                                          "awaiting"))
    parked: dict[str, tuple] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        for tid, status, kind, request, awaiting in conn.execute(
                f"SELECT {pick} FROM tasks t WHERE t.id IN ({ph}) AND t.status IN ('scheduled', 'blocked', 'triage')",
                chunk):
            parked[str(tid)] = (status, kind, request, awaiting)
    if not parked:
        return {}, {}
    events: dict[str, tuple] = {}
    keys = list(parked)
    kph = ",".join("?" for _ in kwi.PARK_EVENT_KINDS)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        ph = ",".join("?" for _ in chunk)
        try:
            for tid, kind, payload, at in conn.execute(
                    f"SELECT e.task_id, e.kind, e.payload, e.created_at FROM task_events e WHERE e.id IN ("
                    f"SELECT MAX(id) FROM task_events WHERE task_id IN ({ph}) AND kind IN ({kph}) GROUP BY task_id)",
                    (*chunk, *kwi.PARK_EVENT_KINDS)):
                events[str(tid)] = (kind, payload, at)
        except sqlite3.Error:  # board predates task_events
            break
    _overlay_contained(conn, parked, events)
    return parked, events


def _overlay_contained(conn: sqlite3.Connection, parked: dict[str, tuple], events: dict[str, tuple]) -> None:
    """A held triage card is described from its ``contained`` episode, not from an older park event. [daveey]"""
    triage = [t for t, row in parked.items() if row[0] == "triage"]
    if not triage:
        return
    try:
        from hermes_cli import kanban_contained_wait
        for tid, info in kanban_contained_wait.read(conn, triage).items():
            events[tid] = ("contained", {"contained": info}, info.get("contained_at"))
    except Exception:  # display-only: the card keeps its ordinary triage description
        logging.getLogger(__name__).debug("contained wait read failed", exc_info=True)


def _describe_waits(parked: dict[str, tuple], events: dict[str, tuple], now: float | None = None,
                    ) -> dict[str, dict[str, Any]]:
    from hermes_cli import kanban_wait_info as kwi

    out: dict[str, dict[str, Any]] = {}
    for tid, (status, kind, request, awaiting) in parked.items():
        ev_kind, payload, at = events.get(tid, (None, None, None))
        if ev_kind == "contained" and isinstance(payload, dict) and status == "triage":
            out[tid] = kwi.describe_contained(payload["contained"], now=now)
            continue
        try:
            human = _stored_needs_human(request, kind)
        except Exception:
            human = False
        info = kwi.describe(status, block_kind=kind, human=human, park=payload, park_kind=ev_kind, since=at,
                            awaiting=awaiting, now=now)
        if info is not None:
            out[tid] = info
    return out
