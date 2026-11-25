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


def _local_wait_info(conn: sqlite3.Connection, task_ids: Iterable[str], now: float | None = None, *, replica: Any = ...,
                     ) -> dict[str, dict[str, Any]]:
    """[daveey] ``{task_id: wait info}`` (``kanban_wait_info.describe``) for the parked cards among ``task_ids``: what
    each waits on, since when, and whether only an agent can end it. One query for the cards, one for their newest
    park events. Missing columns/tables degrade to status-only info."""
    rows = (_local_wait_rows(conn, task_ids) if replica is ...
            else _local_wait_rows(conn, task_ids, replica=replica))
    return _eta_overlay(conn, _external_overlay(conn, _describe_waits(*rows, now=now), now), now)


def _eta_overlay(conn, info, now):
    """CI / deploy waits expect the median of this board's earlier ones (``kanban_wait_history``)."""
    from hermes_cli import kanban_wait_history

    kanban_wait_history.overlay(conn, info, now)
    return info


def _external_overlay(conn, info, now):
    from hermes_cli import kanban_external_activity as kea
    kea.overlay_waits(conn, info, now)
    return info


def _local_wait_rows(conn: sqlite3.Connection, task_ids: Iterable[str], *, replica: Any = ...) -> tuple[dict[str, tuple], dict[str, tuple]]:
    """``(parked, events)``: the stored rows ``_describe_waits`` turns into wait info. Separate so a board memo can
    keep the rows and re-judge their age at each read (the stall cut-off moves with the clock, the rows do not)."""
    from hermes_cli import kanban_wait_info as kwi
    from hermes_state_page_pack import read_rows

    ids = [str(t) for t in dict.fromkeys(task_ids)]
    if not ids:
        return {}, {}
    from hermes_cli.kanban_table_columns import table_columns
    cols = table_columns(conn, "tasks")  # one result row, not one per column
    pick = ", ".join(f"t.{c}" if c in cols else f"NULL AS {c}" for c in ("id", "status", "block_kind", "human_request",
                                                                          "awaiting"))
    # [daveey] Each result row is a GIL release in the Desktop backend (kanban_table_columns), so both reads hand
    # their rows over packed in one (hermes_state_page_pack: types, order and doubles preserved). The session list
    # read ~140 hub parked cards here per board change: ~220 row steps, seconds under the serve's GIL convoy.
    parked: dict[str, tuple] = {}
    for tid, status, kind, request, awaiting in read_rows(conn,
            f"SELECT {pick} FROM tasks t WHERE t.id IN (SELECT value FROM json_each(?)) "
            "AND t.status IN ('scheduled', 'blocked', 'triage')", (json.dumps(ids),), 5):
        parked[str(tid)] = (status, kind, request, awaiting)
    if not parked:
        return {}, {}
    events: dict[str, tuple] = {}
    kph = ",".join("?" for _ in kwi.PARK_EVENT_KINDS)
    try:
        # The newest park event per card, probed per card through (kind, task_id, id): grouped over ``task_id``
        # SQLite walked every event of each card (idx_events_task), ~40 ms on the hub board, for the same ids.
        for tid, kind, payload, at in read_rows(conn,
                "SELECT e.task_id, e.kind, e.payload, e.created_at FROM task_events e WHERE e.id IN ("
                "SELECT (SELECT MAX(x.id) FROM task_events x WHERE x.task_id = j.value "
                f"AND x.kind IN ({kph})) FROM json_each(?) j)", (*kwi.PARK_EVENT_KINDS, json.dumps(list(parked))), 4):
            events[str(tid)] = (kind, payload, at)
    except sqlite3.Error:  # board predates task_events
        pass
    if replica is ...:
        _overlay_contained(conn, parked, events)
    else:
        _overlay_contained(conn, parked, events, replica=replica)
    return parked, events


def _overlay_contained(conn: sqlite3.Connection, parked: dict[str, tuple], events: dict[str, tuple], *, replica: Any = ...) -> None:
    """A held triage card is described from its ``contained`` episode, not from an older park event. [daveey]"""
    triage = [t for t, row in parked.items() if row[0] == "triage"]
    if not triage:
        return
    try:
        from hermes_cli import kanban_contained_display
        rows = (kanban_contained_display.read(conn, triage) if replica is ...
                else kanban_contained_display.read(conn, triage, replica=replica))
        for tid, info in rows.items():
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
