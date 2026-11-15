"""Which parks are a card's own wait, independent of the shared blocker it hit earlier. [daveey]

A card behind wall X that re-parks WITHOUT X's id stays in X's ``behind`` (the park says nothing about the
wall). When that park set a ``wake_check`` of its own (an exact-head CI probe, a deploy), the card waits on that
probe, not on X: X's clear must not wake it early, and once its own probe fires, X must not hold it.

2026-11-15 PST, MacBook board: two landers parked on their CI probes on pool workers (``probe_host`` = the
worker, so the home registered no probe row) and the home's registered check for ``github-daveey-core-reserve``
woke both out of those waits (02:40:44, 03:07:10).
"""
from __future__ import annotations

import sqlite3
from typing import Optional


def probe_elsewhere(conn: sqlite3.Connection, task_id: str) -> bool:
    """The card's current park set a wake_check that another gateway runs (``probe_host``).

    The parking gateway registers and runs that probe and relays its wake home, so this board holds no
    ``task_wake_checks`` row for it (``kanban_wake_check.set_check(register_here=False)``) although the park
    has a probe of its own."""
    from hermes_cli import kanban_wake_check as kwc
    from hermes_cli.kanban_continuation import park_state

    payload = park_state(conn, task_id)["payload"]
    host = payload.get("probe_host")
    return bool(payload.get("wake_check")) and isinstance(host, str) and not kwc.probe_here(conn, host)


def woken_by_own_park(conn: sqlite3.Connection, blocker_id: str, task_id: str, asserted_at: int) -> bool:
    """True when the card's newest wake is its own probe's exit 0 (``condition_met``) and the park that set that
    probe came after the card hit ``blocker_id`` (``asserted_at``) without naming it: an independent wait that is
    now satisfied. One run spends it (a ``claimed`` event after the wake), as one run spends a probe release."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_continuation import SCHEDULED_ENTRY_EVENTS
    from hermes_cli.kanban_shared_blockers import CONDITION_MET_EVENT, normalize_blocker_id

    met = conn.execute(
        "SELECT id, created_at, payload FROM task_events WHERE kind = ? AND task_id = ? "
        "ORDER BY id DESC LIMIT 1", (CONDITION_MET_EVENT, task_id)).fetchone()
    if met is None or (kb._to_epoch(met["created_at"]) or 0) < asserted_at:
        return False
    marks = ",".join("?" * len(SCHEDULED_ENTRY_EVENTS))
    park = conn.execute(
        f"SELECT created_at, payload FROM task_events WHERE task_id = ? AND kind IN ({marks}) AND id < ? "
        "ORDER BY id DESC LIMIT 1", (task_id, *SCHEDULED_ENTRY_EVENTS, int(met["id"]))).fetchone()
    if park is None or (kb._to_epoch(park["created_at"]) or 0) < asserted_at:
        return False
    payload = kb._json_dict(park["payload"])
    awaiting = payload.get("awaiting")
    if not isinstance(awaiting, dict):
        awaiting = {}
    if blocker_id in (normalize_blocker_id(payload.get("blocker_id")),
                      normalize_blocker_id(awaiting.get("shared_blocker"))):
        return False
    probe: Optional[str] = payload.get("wake_check")
    fired = kb._json_dict(met["payload"]).get("wake_check")
    if not probe or (fired and fired != probe):
        return False
    return conn.execute(
        "SELECT 1 FROM task_events WHERE kind = 'claimed' AND task_id = ? AND id > ? LIMIT 1",
        (task_id, int(met["id"]))).fetchone() is None
