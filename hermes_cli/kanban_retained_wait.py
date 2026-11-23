"""A retained (scope-stopped) triage card whose wait is explained is agent work, not a human ask. [daveey]

``kanban_retain`` parks a card in ``triage`` (``decision_retained``). With no live owner the stranded-park sweep
(``kanban_stranded_park``) writes a generic "decide this parked card" ``human_request`` on it, and the session stripe
went red (``human_ask``) although the card's open parent, or the wait recorded by its ``kanban schedule --awaiting``,
already said what it waits for (t_01a1ee476b0f4c0b, 2026-11-22).

:func:`explanation` answers, for one triage card, what explains its wait: an open parent, a recorded awaiting card
or clock, or None. It applies only to a card whose latest run ended in ``decision_retained`` and only to the
request the sweep generated (its ``stranded_park_escalated`` event is newer than the retain); a request anyone else
filed stays a human ask.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

RETAINED = "decision_retained"
SWEEP_EVENT = "stranded_park_escalated"
_CLOSED = ("done", "archived")
_PARKS = ("scheduled", "continued")
_WAITS = ("card", "clock")


def _latest(conn: sqlite3.Connection, task_id: str, kinds: tuple[str, ...]) -> Optional[tuple[int, str, str]]:
    marks = ",".join("?" for _ in kinds)
    return conn.execute(f"SELECT id, kind, payload FROM task_events WHERE task_id=? AND kind IN ({marks}) "
                        "ORDER BY id DESC LIMIT 1", (task_id, *kinds)).fetchone()


def _open_parent(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT l.parent_id FROM task_links l LEFT JOIN tasks p ON p.id = l.parent_id WHERE l.child_id=? "
        f"AND COALESCE(p.status, '') NOT IN ({','.join('?' for _ in _CLOSED)}) ORDER BY l.parent_id LIMIT 1",
        (task_id, *_CLOSED)).fetchone()
    return row[0] if row else None


def _recorded_wait(conn: sqlite3.Connection, task_id: str, after: int) -> Optional[str]:
    """The awaiting card (not yet closed) or clock a park recorded after the retain, as ``card:<id>``/``clock``."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind IN (?, ?) AND id > ? "
                       "ORDER BY id DESC LIMIT 1", (task_id, *_PARKS, after)).fetchone()
    try:
        awaiting = (json.loads(row[0] or "{}") if row else {}).get("awaiting")
    except ValueError:
        return None
    if not isinstance(awaiting, dict) or awaiting.get("kind") not in _WAITS:
        return None
    if awaiting["kind"] == "clock":
        return "clock"
    ref = str(awaiting.get("ref") or "").strip()
    if not ref:
        return None
    closed = conn.execute("SELECT status FROM tasks WHERE id=?", (ref,)).fetchone()
    return None if closed is not None and closed[0] in _CLOSED else f"card:{ref}"


def explanation(conn: sqlite3.Connection, task_id: str, *, generated_only: bool = True) -> Optional[str]:
    """What explains this triage card's wait (``card:<id>`` or ``clock``), or None when nothing does.

    ``generated_only``: apply only while the card's ask is the sweep's generic one. A card with no such ask
    (nothing to suppress) still gets its explanation, which the generator uses to leave it alone."""
    retained = _latest(conn, task_id, (RETAINED, "claimed", "promoted_manual", "unblocked"))
    if retained is None or retained[1] != RETAINED:
        return None
    if generated_only:
        swept = _latest(conn, task_id, (SWEEP_EVENT,))
        if swept is None or swept[0] < retained[0]:
            return None
    parent = _open_parent(conn, task_id)
    if parent:
        return f"card:{parent}"
    return _recorded_wait(conn, task_id, retained[0])


def annotate(conn: sqlite3.Connection, found: dict[str, dict[str, Any]]) -> None:
    """Stamp ``retained_wait`` on the triage cards of ``found`` (id -> row) that an open parent or recorded wait
    explains. Missing tables degrade to no annotation: the card then stays an ask."""
    for tid, row in found.items():
        if row.get("status") != "triage":
            continue
        try:
            why = explanation(conn, tid)
        except sqlite3.Error:
            continue
        if why:
            row["retained_wait"] = why
